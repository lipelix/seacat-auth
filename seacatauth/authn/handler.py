import datetime
import logging

import asab
import asab.web.rest

import aiohttp.web

import urllib.parse
import jwcrypto.jwk

from ..audit import AuditCode
from ..cookie import set_cookie, delete_cookie

#

L = logging.getLogger(__name__)

#


class AuthenticationHandler(object):
	"""
	Login and authentication

	---
	tags: ["Login and authentication"]
	"""

	def __init__(self, app, authn_svc):
		self.App = app
		self.AuthenticationService = authn_svc
		self.CredentialsService = app.get_service('seacatauth.CredentialsService')
		self.SessionService = app.get_service('seacatauth.SessionService')
		self.CookieService = app.get_service('seacatauth.CookieService')
		self.AuditService = app.get_service('seacatauth.AuditService')
		self.BatmanService = app.BatmanService
		self.CommunicationService = app.get_service('seacatauth.CommunicationService')

		web_app = app.WebContainer.WebApp
		web_app.router.add_put(r'/public/login.prologue', self.login_prologue)
		web_app.router.add_put(r'/public/login/{lsid}', self.login)
		web_app.router.add_put(r'/public/login/{lsid}/smslogin', self.smslogin)
		web_app.router.add_put(r'/public/login/{lsid}/webauthn', self.webauthn_login)
		web_app.router.add_put(r'/public/logout', self.logout)

		# Public endpoints
		web_app_public = app.PublicWebContainer.WebApp
		web_app_public.router.add_put(r'/public/login.prologue', self.login_prologue)
		web_app_public.router.add_put(r'/public/login/{lsid}', self.login)
		web_app_public.router.add_put(r'/public/login/{lsid}/smslogin', self.smslogin)
		web_app_public.router.add_put(r'/public/login/{lsid}/webauthn', self.webauthn_login)
		web_app_public.router.add_put(r'/public/logout', self.logout)

	@asab.web.rest.json_schema_handler({
		"type": "object",
		"required": ["ident"],
		"properties": {
			"ident": {
				"type": "string",
				"description": "Value (usually email or username) used for locating credentials to be used for login."},
			"qs": {
				"type": "string",
				"description":
					"Optional extra parameters used for locating credentials. "
					"Allowed parameter names must be listed in `[seacatauth:authentication] custom_login_parameters` "
					"in the app configuration."}}
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
		ident = json_data.get('ident')

		# Get arguments specified in login URL query
		expiration = None
		login_preferences = None
		query_string = json_data.get("qs")
		if query_string is None:
			login_dict = None
		else:
			query_dict = urllib.parse.parse_qs(query_string)

			login_dict = {
				k: v
				for k, v in query_dict.items()
				if k in self.AuthenticationService.CustomLoginParameters}

			# Get requested session expiration
			# TODO: This option should be moved to client config or removed completely
			expiration = query_dict.get("expiration")
			if expiration is not None:
				try:
					expiration = float(expiration[0])
				except Exception as e:
					L.warning("Error when parsing expiration: {}".format(e))

			# Get preferred login descriptor IDs
			# TODO: This option should be moved to client config or removed completely
			login_preferences = query_dict.get("ldid")

		# Locate credentials
		credentials_id = await self.CredentialsService.locate(ident, stop_at_first=True, login_dict=login_dict)
		if credentials_id is None or credentials_id == []:
			L.warning("Cannot locate credentials.", struct_data={"ident": ident})
			# Empty credentials is used for creating a fake login session
			credentials_id = ""

		# Deny login to m2m credentials
		if credentials_id != "":
			cred_provider = self.CredentialsService.get_provider(credentials_id)
			if cred_provider.Type == "m2m":
				L.warning("Cannot login with machine credentials.", struct_data={"cid": credentials_id})
				# Empty credentials is used for creating a fake login session
				credentials_id = ""

		# Prepare login descriptors
		login_descriptors = None
		if credentials_id != "":
			login_descriptors = await self.AuthenticationService.prepare_login_descriptors(
				credentials_id=credentials_id,
				request_headers=request.headers,
				login_preferences=login_preferences
			)

		if login_descriptors is None:
			# Prepare fallback login descriptors for fake login session
			credentials_id = ""
			L.warning("Creating fake login session.", struct_data={"ident": ident})
			login_descriptors = await self.AuthenticationService.prepare_fallback_login_descriptors(
				credentials_id=credentials_id,
				request_headers=request.headers
			)

		if login_descriptors is None:
			L.error("Fatal error: Failed to prepare fallback login descriptors.", struct_data={"ident": ident})
			raise aiohttp.web.HTTPInternalServerError()

		login_session = await self.AuthenticationService.create_login_session(
			credentials_id=credentials_id,
			client_public_key=key.get_op_key("encrypt"),  # extract EC public key from JWT
			login_descriptors=login_descriptors,
			ident=ident,
			requested_session_expiration=expiration,
		)

		key = jwcrypto.jwk.JWK.from_pyca(login_session.PublicKey)

		response = {
			'lsid': login_session.Id,
			'lds': [descriptor.serialize() for descriptor in login_descriptors],
			'key': key.export_public(as_dict=True),
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
		except KeyError:
			L.warning("Login failed: Invalid login session ID", struct_data={
				"lsid": lsid
			})
			return asab.web.rest.json_response(
				request,
				data={'result': 'FAILED'},
				status=401
			)

		if login_session.RemainingLoginAttempts <= 0:
			await self.AuthenticationService.delete_login_session(lsid)
			L.warning("Login failed: no more attempts", struct_data={
				"lsid": lsid,
				"ident": login_session.Ident,
				"cid": login_session.CredentialsId
			})
			return asab.web.rest.json_response(
				request,
				data={'result': 'FAILED'},
				status=401
			)

		await self.AuthenticationService.update_login_session(
			lsid,
			remaining_login_attempts=login_session.RemainingLoginAttempts - 1
		)

		request_data = login_session.decrypt(await request.read())
		L.debug("Processing login attempt", struct_data={"payload": request_data, "lsid": login_session.Id})

		request_data["request_headers"] = request.headers

		access_ips = [request.remote]
		ff = request.headers.get('X-Forwarded-For')
		if ff is not None:
			access_ips.extend(ff.split(', '))

		# Get the current session's TrackID
		current_session = await self.CookieService.get_session_by_sci(request)
		if current_session is not None:
			track_id = current_session.TrackId
		else:
			track_id = None

		authenticated = await self.AuthenticationService.authenticate(login_session, request_data)

		if not authenticated:
			# TODO: Log also the IP address
			await self.AuditService.append(
				AuditCode.LOGIN_FAILED,
				{
					'cid': login_session.CredentialsId,
					'ips': access_ips,
				}
			)

			L.warning("Login failed: authentication failed", struct_data={
				"lsid": lsid,
				"ident": login_session.Ident,
				"cid": login_session.CredentialsId
			})

			self.AuthenticationService.LoginCounter.add('failed', 1)

			return asab.web.rest.json_response(
				request,
				data={'result': 'FAILED'},
				status=401
			)

		# Do the actual login
		session = await self.AuthenticationService.login(login_session, from_info=access_ips, track_id=track_id)

		# TODO: Note the last successful login time
		# TODO: Log also the IP address
		body = {
			'result': 'OK',
			'cid': login_session.CredentialsId,
			'sid': str(session.Session.Id),
		}

		response = aiohttp.web.Response(
			body=login_session.encrypt(body)
		)

		set_cookie(self.App, response, session)

		self.AuthenticationService.LoginCounter.add('successful', 1)

		return response

	async def logout(self, request):
		"""
		Log out of the current session and all its subsessions
		"""
		session = await self.CookieService.get_session_by_sci(request)
		if session is None:
			raise aiohttp.web.HTTPBadRequest()

		await self.SessionService.delete(session.Session.Id)

		redirect_uri = request.query.get("redirect_uri")
		if redirect_uri is not None:
			response = aiohttp.web.HTTPFound(redirect_uri)
		else:
			response = asab.web.rest.json_response(request, {'result': 'OK'})

		delete_cookie(self.App, response)

		if self.BatmanService is not None:
			response.del_cookie(self.BatmanService.CookieName)

		return response

	async def smslogin(self, request):
		"""
		Generate a one-time passcode and send it via SMS
		"""
		# Decode JSON request
		lsid = request.match_info["lsid"]
		login_session = await self.AuthenticationService.get_login_session(lsid)
		if login_session is None:
			L.error("Login session not found.", struct_data={"lsid": lsid})
			raise aiohttp.web.HTTPUnauthorized()

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
			L.error("Login session not found.", struct_data={"lsid": lsid})
			raise aiohttp.web.HTTPUnauthorized()

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

		await self.AuthenticationService.update_login_session(lsid, data=login_data)

		return aiohttp.web.Response(body=login_session.encrypt(authentication_options))


	async def _get_client_login_key(self, client_id):
		client_service = self.AuthenticationService.App.get_service("seacatauth.ClientService")
		try:
			client = await client_service.get(client_id)
			login_key = client.get("login_key")
		except KeyError:
			login_key = None
		return login_key
