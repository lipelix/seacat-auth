import datetime
import logging

import asab
import asab.web.rest
import asab.exceptions

import aiohttp.web

import urllib.parse
import jwcrypto.jwk

from .. import exceptions, AuditLogger, generic
from ..last_activity import EventCode
from ..cookie import set_cookie, delete_cookie
from ..decorators import access_control
from ..openidconnect.utils import AUTHORIZE_PARAMETERS

#

L = logging.getLogger(__name__)

#


JWK_PARAMS = {
	"crv": {"type": "string"},
	"ext": {"type": "boolean"},
	"key_ops": {"type": "array"},
	"kty": {"type": "string"},
	"x": {"type": "string"},
	"y": {"type": "string"}
}


class AuthenticationHandler(object):
	"""
	Login and authentication

	---
	tags: ["Login and authentication"]
	"""

	def __init__(self, app, authn_svc):
		self.App = app
		self.AuthenticationService = authn_svc
		self.CredentialsService = app.get_service("seacatauth.CredentialsService")
		self.SessionService = app.get_service("seacatauth.SessionService")
		self.CookieService = app.get_service("seacatauth.CookieService")
		self.BatmanService = app.BatmanService
		self.CommunicationService = app.get_service("seacatauth.CommunicationService")

		web_app = app.WebContainer.WebApp
		web_app.router.add_put(r"/public/login.prologue", self.login_prologue)
		web_app.router.add_put(r"/public/login/{lsid}", self.login)
		web_app.router.add_put(r"/public/login/{lsid}/smslogin", self.smslogin)
		web_app.router.add_put(r"/public/login/{lsid}/webauthn", self.webauthn_login)
		web_app.router.add_put(r"/public/logout", self.logout)
		web_app.router.add_put("/impersonate", self.impersonate)
		web_app.router.add_post("/impersonate", self.impersonate_and_redirect)

		# Public endpoints
		web_app_public = app.PublicWebContainer.WebApp
		web_app_public.router.add_put(r"/public/login.prologue", self.login_prologue)
		web_app_public.router.add_put(r"/public/login/{lsid}", self.login)
		web_app_public.router.add_put(r"/public/login/{lsid}/smslogin", self.smslogin)
		web_app_public.router.add_put(r"/public/login/{lsid}/webauthn", self.webauthn_login)
		web_app_public.router.add_put(r"/public/logout", self.logout)
		web_app_public.router.add_put("/impersonate", self.impersonate)
		web_app_public.router.add_post("/impersonate", self.impersonate_and_redirect)


	@asab.web.rest.json_schema_handler({
		"type": "object",
		"required": ["ident", *JWK_PARAMS.keys()],
		"properties": {
			"ident": {
				"type": "string",
				"description": "Value (usually email or username) used for locating credentials to be used for login."},
			"qs": {
				"type": "string",
				"description":
					"Optional extra parameters used for locating credentials. "
					"Allowed parameter names must be listed in `[seacatauth:authentication] custom_login_parameters` "
					"in the app configuration."},
			**JWK_PARAMS
		}
	})
	async def login_prologue(self, request, *, json_data):
		"""
		Locate credentials by `ident` and establish an encrypted login session

		Flow:
		- Locate credentials by ident
		- Get app's available login descriptors
		- Remove login descriptors that the credentials cannot use
		- Store the login data in a new LoginSession object
		- Respond with login session ID, encryption key and available login descriptors
		"""
		key = jwcrypto.jwk.JWK.from_json(await request.read())
		ident = json_data.get("ident")
		login_session_id = json_data.get("lsid")

		# Get arguments specified in login URL query
		login_preferences = None
		query_string = json_data.get("qs")
		if query_string is None:
			login_dict = None
		else:
			query_dict = urllib.parse.parse_qs(query_string)

			login_dict = {}
			for k, v in query_dict.items():
				if k in self.AuthenticationService.CustomLoginParameters:
					if len(v) > 1:
						raise asab.exceptions.ValidationError("Repeated query parameters are not supported")
					login_dict[k] = v[0]

			# Get preferred login descriptor IDs
			# TODO: This option should be moved to client config or removed completely
			login_preferences = query_dict.get("ldid")

		if login_session_id:
			login_session = await self.AuthenticationService.get_login_session(login_session_id)
		else:
			login_session = await self.AuthenticationService.create_login_session()

		if credentials_id != "":
			# M2M credentials produce a fake login session
			cred_provider = self.CredentialsService.get_provider(credentials_id)
			if cred_provider.Type == "m2m":
				L.warning("Cannot login with machine credentials.", struct_data={"cid": credentials_id})
				# Empty credentials is used for creating a fake login session
				credentials_id = ""

			# Suspended credentials produce a fake login session
			credentials = await self.CredentialsService.get(credentials_id)
			if credentials.get("suspended") is True:
				L.warning("Login denied to suspended credentials", struct_data={"cid": credentials_id})
				credentials_id = ""

		login_session = await self.AuthenticationService.prepare_seacat_login(
			login_session=login_session,
			ident=ident,
			client_public_key=key.get_op_key("encrypt"),
			request_headers=request.headers,
			login_dict=login_dict,
			login_preferences=login_preferences
		)
		if login_session is None:
			login_session = await self.AuthenticationService.prepare_failed_seacat_login(
				login_session=login_session,
				ident=ident,
				client_public_key=key.get_op_key("encrypt")
			)

		print(login_session)

		key = jwcrypto.jwk.JWK.from_pyca(login_session.SeacatLogin.ServerPublicKey)

		response = {
			"lsid": login_session.Id,
			"lds": [
				descriptor.serialize()
				for descriptor in login_session.SeacatLogin.LoginDescriptors],
			"key": key.export_public(as_dict=True),
		}
		return asab.web.rest.json_response(request, response)


	async def login(self, request):
		"""
		Perform an encrypted login request

		Flow:
		- Locate login session by it ID
		- Check if there are any login attempts remaining
		- Record login attempt
		- Validate login request data
		- If valid, log the user in
		"""
		lsid = request.match_info["lsid"]

		try:
			login_session = await self.AuthenticationService.get_login_session(lsid)
		except KeyError as e:
			print(e)
			L.log(asab.LOG_NOTICE, "Login failed: Invalid login session ID", struct_data={
				"lsid": lsid
			})
			return asab.web.rest.json_response(
				request,
				data={"result": "FAILED"},
				status=401
			)

		if login_session.SeacatLogin.LoginAttemptsLeft <= 0:
			await self.AuthenticationService.delete_login_session(lsid)
			L.log(asab.LOG_NOTICE, "Login failed: No more attempts", struct_data={
				"lsid": lsid,
				"ident": login_session.Ident,
				"cid": login_session.CredentialsId
			})
			return asab.web.rest.json_response(
				request,
				data={"result": "FAILED"},
				status=401
			)

		login_session = await self.AuthenticationService.update_login_session(
			login_session,
			login_attempts_left=login_session.SeacatLogin.LoginAttemptsLeft - 1
		)

		request_data = login_session.decrypt(await request.read())
		L.debug("Processing login attempt", struct_data={"payload": request_data, "lsid": login_session.Id})

		request_data["request_headers"] = request.headers

		access_ips = generic.get_request_access_ips(request)

		authenticated = await self.AuthenticationService.authenticate(login_session, request_data)

		if not authenticated:
			AuditLogger.log(asab.LOG_NOTICE, "Authentication failed", struct_data={
				"cid": login_session.CredentialsId,
				"lsid": lsid,
				"ident": login_session.Ident,
				"from_ip": access_ips
			})
			await self.AuthenticationService.LastActivityService.update_last_activity(
				EventCode.LOGIN_FAILED, login_session.CredentialsId, from_ip=access_ips)

			self.AuthenticationService.LoginCounter.add("failed", 1)

			return asab.web.rest.json_response(
				request,
				data={"result": "FAILED"},
				status=401
			)

		# If there already is a root session with the same credentials ID, refresh it instead of creating a new one
		if request.Session is not None and request.Session.Credentials.Id == login_session.CredentialsId:
			root_session = request.Session
		else:
			root_session = None

		# Do the actual login
		session = await self.AuthenticationService.login(
			login_session, root_session=root_session, from_info=access_ips)

		# TODO: Note the last successful login time
		# TODO: Log also the IP address
		body = {
			'result': 'OK',
			'cid': login_session.SeacatLogin.CredentialsId,
			'sid': str(session.Session.Id),
		}

		response = aiohttp.web.Response(
			body=login_session.SeacatLogin.encrypt(body)
		)

		cookie_domain = None
		if hasattr(login_session.SeacatLogin, "ClientId"):
			client_svc = self.App.get_service("seacatauth.ClientService")
			try:
				client = await client_svc.get(login_session.ClientId)
				cookie_domain = client.get("cookie_domain")
			except KeyError:
				L.error("Client not found.", struct_data={"client_id": login_session.ClientId})
		if cookie_domain is None:
			cookie_domain = self.CookieService.RootCookieDomain

		set_cookie(self.App, response, session, cookie_domain)

		self.AuthenticationService.LoginCounter.add('successful', 1)

		return response


	async def logout(self, request):
		"""
		Log out of the current session and all its subsessions
		"""
		try:
			session = await self.CookieService.get_session_by_request_cookie(request)
		except exceptions.NoCookieError:
			L.log(asab.LOG_NOTICE, "Unauthorized: No root cookie in request")
			return aiohttp.web.HTTPBadRequest()
		except exceptions.SessionNotFoundError:
			L.log(asab.LOG_NOTICE, "Unauthorized: Request cookie matched no active session")
			return aiohttp.web.HTTPBadRequest()

		await self.SessionService.delete(session.Session.Id)

		redirect_uri = request.query.get("redirect_uri")
		if redirect_uri is not None:
			response = aiohttp.web.HTTPFound(redirect_uri)
		else:
			response = asab.web.rest.json_response(request, {'result': 'OK'})

		delete_cookie(self.App, response)

		# If the current session is impersonated and the original session has a
		# root cookie, try to restore the original session
		if session.Authentication.ImpersonatorSessionId is not None:
			try:
				impersonator_session = await self.SessionService.get(session.Authentication.ImpersonatorSessionId)
			except KeyError:
				L.log(asab.LOG_NOTICE, "Impersonator session not found", struct_data={
					"sid": session.Authentication.ImpersonatorSessionId})
			else:
				if impersonator_session.Cookie is None:
					# Case when the impersonation was started by an M2M session, which has no cookie
					pass
				else:
					set_cookie(self.App, response, impersonator_session)

		AuditLogger.log(asab.LOG_NOTICE, "Logout successful", struct_data={
			"cid": session.Credentials.Id, "sid": session.SessionId, "token_type": "cookie"})

		return response


	async def smslogin(self, request):
		"""
		Generate a one-time passcode and send it via SMS
		"""
		# Decode JSON request
		lsid = request.match_info["lsid"]
		login_session = await self.AuthenticationService.get_login_session(lsid)
		if login_session is None:
			L.log(asab.LOG_NOTICE, "Login session not found", struct_data={"lsid": lsid})
			return aiohttp.web.HTTPUnauthorized()

		json_body = login_session.decrypt(await request.read())

		# Initiate SMS login
		success = False
		factor_id = json_body.get("factor_id")
		if factor_id is not None:
			sms_factor = self.AuthenticationService.get_login_factor(factor_id)
			if sms_factor is not None:
				success = await sms_factor.send_otp(login_session)
			else:
				L.error("Login factor not found", struct_data={"factor_id": factor_id})
		else:
			L.error("factor_id not specified", struct_data={"factor_id": factor_id})

		body = {"result": "OK" if success is True else "FAILED"}
		return aiohttp.web.Response(body=login_session.encrypt(body))


	async def webauthn_login(self, request):
		"""
		Initialize WebAuthn challenge and return WebAuthn authentication options object
		"""
		# Decode JSON request
		lsid = request.match_info["lsid"]
		login_session = await self.AuthenticationService.get_login_session(lsid)
		if login_session is None:
			L.log(asab.LOG_NOTICE, "Login session not found", struct_data={"lsid": lsid})
			return aiohttp.web.HTTPUnauthorized()

		json_body = login_session.decrypt(await request.read())

		# descriptor_id = json_body.get("descriptor_id")
		factor_type = json_body.get("factor_type")
		if factor_type != "webauthn":
			body = {"result": "FAILED", "message": "Unsupported factor type."}
			return aiohttp.web.Response(body=login_session.encrypt(body))

		# Webauthn challenge timeout should be the same as the current login session timeout
		timeout = (login_session.ExpiresAt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() * 1000

		webauthn_svc = self.AuthenticationService.App.get_service("seacatauth.WebAuthnService")
		authentication_options = await webauthn_svc.get_authentication_options(
			login_session.CredentialsId,
			timeout
		)

		login_data = login_session.Data
		login_data["webauthn"] = authentication_options

		login_session = await self.AuthenticationService.update_login_session(login_session, data=login_data)

		return aiohttp.web.Response(body=login_session.encrypt(authentication_options))


	async def _get_client_login_key(self, client_id):
		client_service = self.AuthenticationService.App.get_service("seacatauth.ClientService")
		try:
			client = await client_service.get(client_id)
			login_key = client.get("login_key")
		except KeyError:
			login_key = None
		return login_key


	@asab.web.rest.json_schema_handler({
		"type": "object",
		"required": ["credentials_id"],
		"properties": {
			"credentials_id": {
				"type": "string",
				"description": "Credentials ID of the impersonation target."},
			"expiration": {
				"oneOf": [{"type": "string"}, {"type": "number"}],
				"description":
					"Expiration of the impersonated session. The value can be either the number of seconds "
					"or a time-unit string such as '4 h' or '3 d'."}},
		"example": {
			"credentials_id": "mongodb:default:abc123def456",
			"expiration": "5m"}
	})
	@access_control("authz:impersonate")
	async def impersonate(self, request, *, json_data):
		"""
		Open a root session impersonated as a different user.
		Response contains a Set-Cookie header with the new root session cookie.

		Requires `authz:impersonate`.
		"""
		from_info = [request.remote]
		ff = request.headers.get("X-Forwarded-For")
		if ff is not None:
			from_info.extend(ff.split(", "))

		target_cid = json_data["credentials_id"]
		if request.Session.Session.ParentSessionId is None:
			impersonator_root_session = request.Session
		else:
			impersonator_root_session = await self.SessionService.get(request.Session.Session.ParentSessionId)

		session = await self._impersonate(impersonator_root_session, from_info, target_cid)
		response = asab.web.rest.json_response(request, {"result": "OK"})
		set_cookie(self.App, response, session, cookie_domain=self.CookieService.RootCookieDomain)
		return response


	@access_control("authz:impersonate")
	async def impersonate_and_redirect(self, request):
		"""
		Open a root session impersonated as a different user. Response contains a Set-Cookie header with the new
		root session cookie and redirection to the authorize endpoint. This effectively overwrites user's current
		root cookie. Reference to current root session is kept in the impersonated session.
		On logout, the original root cookie is set again.

		Requires `authz:impersonate`.
		---
		requestBody:
			content:
				application/x-www-form-urlencoded:
					schema:
						type: object
						properties:
							credentials_id:
								type: string
								description: Credentials ID of the impersonation target.
							client_id:
								type: string
								description:
							redirect_uri:
								type: string
								description:
									URI of the client app to redirect to when the impersonation authorization
									is complete.
							response_type:
								type: string
								description: OAuth response type.
							scope:
								type: string
								description: OAuth scope.
						required:
							- credentials_id
							- client_id
							- redirect_uri
						additionalProperties: True
		"""
		oidc_service = self.App.get_service("seacatauth.OpenIdConnectService")
		client_service = self.App.get_service("seacatauth.ClientService")

		from_info = [request.remote]
		ff = request.headers.get("X-Forwarded-For")
		if ff is not None:
			from_info.extend(ff.split(", "))

		request_data = await request.post()
		target_cid = request_data["credentials_id"]
		if request.Session.Session.Type == "root":
			impersonator_root_session = request.Session
		else:
			impersonator_root_session = await self.SessionService.get(request.Session.Session.ParentSessionId)

		session = await self._impersonate(impersonator_root_session, from_info, target_cid)

		client_dict = await client_service.get(request_data["client_id"])
		query = {
			k: v for k, v in request_data.items()
			if k in AUTHORIZE_PARAMETERS}
		authorize_uri = oidc_service.build_authorize_uri(client_dict, **query)

		response = aiohttp.web.HTTPFound(
			authorize_uri,
			headers={
				"Location": authorize_uri,
				"Refresh": "0;url={}".format(authorize_uri),
			},
			content_type="text/html",
			text="""<!doctype html>\n<html lang="en">\n<head></head><body>...</body>\n</html>\n"""
		)
		set_cookie(self.App, response, session, cookie_domain=self.CookieService.RootCookieDomain)
		return response


	async def _impersonate(self, impersonator_root_session, impersonator_from_info, target_cid):
		"""
		Create a new impersonated session and log the event.
		"""
		# TODO: Restrict impersonation based on agent X target resource intersection
		impersonator_cid = impersonator_root_session.Credentials.Id
		try:
			session = await self.AuthenticationService.create_impersonated_session(
				impersonator_root_session, target_cid)
		except exceptions.AccessDeniedError:
			AuditLogger.warning("Impersonation failed: Access denied", struct_data={
				"impersonator_cid": impersonator_cid,
				"impersonator_sid": impersonator_root_session.SessionId,
				"target_cid": target_cid,
				"from_ip": impersonator_from_info,
			})
			return aiohttp.web.HTTPForbidden()
		except Exception as e:
			AuditLogger.exception("Impersonation failed: Unexpected error ({})".format(e), struct_data={
				"impersonator_cid": impersonator_cid,
				"impersonator_sid": impersonator_root_session.SessionId,
				"target_cid": target_cid,
				"from_ip": impersonator_from_info,
			})
			return aiohttp.web.HTTPForbidden()
		else:
			AuditLogger.log(asab.LOG_NOTICE, "Impersonation successful", struct_data={
				"impersonator_cid": impersonator_cid,
				"impersonator_sid": impersonator_root_session.SessionId,
				"target_cid": target_cid,
				"target_sid": str(session.Session.Id),
				"from_ip": impersonator_from_info,
			})
		return session
