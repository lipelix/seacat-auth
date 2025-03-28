import json
import re
import typing
import urllib.parse
import logging
import contextlib
import asab
import aiohttp
import aiohttp.web
import jwcrypto.jwt
import jwcrypto.jwk
import jwcrypto.jws

from ..exceptions import ExternalOAuthFlowError


L = logging.getLogger(__name__)


class GenericOAuth2Login(asab.Configurable):
	"""
	Generic OAuth2 (OpenID) login provider

	Example config:
	```conf
	[seacatauth:oauth2:auth_provider_name]
	; ALL fields must be configured
	client_id=308u2fXEBUTolb.provider.auth
	client_secret=5TfIjab8EZtixx3XkmFLfdXiHxkU2KlU

	issuer=https://provider.auth/login
	discovery_uri=https://provider.auth/login/.well-known/openid-configuration
	jwks_uri=https://provider.auth/login/.well-known/jwks.json
	authorization_endpoint=https://provider.auth/login/oauth/authorize
	token_endpoint=https://provider.auth/login/oauth/token
	userinfo_endpoint=https://provider.auth/login/oauth/userinfo

	scope=openid name email
	label=Login via provider.auth
	```

	Seacat Auth external login callback endpoint (/public/ext-login/callback) must be allowed as a redirect URIs
	in the OAuth client settings at the external login account provider.
	The full callback URL is canonically in the following format:
	https://{my_domain}/api/seacat-auth/public/ext-login/callback
	"""

	Type = None

	def __init__(self, external_login_svc, config_section_name, config=None):
		# TODO: Get the URLs automatically from the discovery_uri (or issuer name)
		super().__init__(config_section_name, config)
		if self.Type is None:
			match = re.match("seacatauth:oauth2:([_a-zA-Z0-9]+)", config_section_name)
			self.Type = match.group(1)

		# Adopt proper OAuth/OpenID terminology
		if "authorize_uri" in self.Config:
			asab.LogObsolete.warning(
				"The 'authorize_uri' config option will be obsoleted. Use 'authorization_endpoint' instead. ",
				struct_data={"eol": "2024-01-31"})
			self.Config["authorization_endpoint"] = self.Config["authorize_uri"]
		if "access_token_uri" in self.Config:
			asab.LogObsolete.warning(
				"The 'access_token_uri' config option will be obsoleted. Use 'token_endpoint' instead. ",
				struct_data={"eol": "2024-01-31"})
			self.Config["token_endpoint"] = self.Config["access_token_uri"]
		if "userinfo_uri" in self.Config:
			asab.LogObsolete.warning(
				"The 'userinfo_uri' config option will be obsoleted. Use 'userinfo_endpoint' instead. ",
				struct_data={"eol": "2024-01-31"})
			self.Config["userinfo_endpoint"] = self.Config["userinfo_uri"]

		self.Issuer = self.Config.get("issuer")
		self.DiscoveryUri = self.Config.get("discovery_uri")
		self.JwksUri = self.Config.get("jwks_uri")

		self.AuthorizationEndpoint = self.Config.get("authorization_endpoint")
		assert self.AuthorizationEndpoint is not None

		self.TokenEndpoint = self.Config.get("token_endpoint")
		assert self.TokenEndpoint is not None

		self.ClientId = self.Config.get("client_id")
		assert self.ClientId is not None

		self.ClientSecret = self.Config.get("client_secret")

		self.Scope = self.Config.get("scope")
		assert self.Scope is not None

		self.Ident = self.Config.get("ident", "email")
		assert self.Ident is not None

		# Label for "Sign up with {ext_login_provider}" button
		# TODO: Make this i18n-compatible (like login descriptors)
		# TODO: Separate label for "Add external login" button
		self.Label = self.Config.get("label")
		assert self.Label is not None

		self.JwkSet = None

		# The URL to return to after successful external login
		# Mostly for debugging purposes
		if "_callback_url" in self.Config:
			self.CallbackUrl = self.Config.get("_callback_url")
		else:
			self.CallbackUrl = external_login_svc.CallbackUrlTemplate.format(provider_type=self.Type)


	async def initialize(self, app):
		await self._prepare_jwks()


	def acr_value(self) -> str:
		"""
		Authentication Context Class Reference (ACR)
		https://openid.net/specs/openid-connect-core-1_0.html#AuthRequest

		OpenID Connect clients may use an ACR value in the authorization request to specifically request which external
		login provider should be used for End-User authentication.
		"""
		return "ext:{}".format(self.Type)


	async def _prepare_jwks(self, speculative=True):
		if not self.JwksUri:
			return
		if self.JwkSet and speculative:
			return
		async with aiohttp.ClientSession() as session:
			async with session.get(self.JwksUri) as resp:
				if resp.status != 200:
					text = await resp.text()
					L.error(
						"Failed to fetch server JWK set: External identity provider responded with error.",
						struct_data={
							"provider": self.Type,
							"status": resp.status,
							"url": resp.url,
							"text": text})
					return
				jwks = await resp.text()
		self.JwkSet = jwcrypto.jwk.JWKSet.from_json(jwks)
		L.info("Identity provider public JWK set loaded.", struct_data={"type": self.Type})

	def get_authorize_uri(
		self, redirect_uri: typing.Optional[str] = None,
		state: typing.Optional[str] = None,
		nonce: typing.Optional[str] = None
	):
		query_params = [
			("response_type", "code"),
			("client_id", self.ClientId),
			("scope", self.Scope),
			("redirect_uri", redirect_uri or self.CallbackUrl),
			("prompt", "select_account"),
		]
		if state is not None:
			query_params.append(("state", state))
		if nonce is not None:
			query_params.append(("nonce", nonce))
		return "{authorize_uri}?{query_string}".format(
			authorize_uri=self.AuthorizationEndpoint,
			query_string=urllib.parse.urlencode(query_params)
		)

	@contextlib.asynccontextmanager
	async def token_request(self, code: str, redirect_uri: str | None = None):
		"""
		Send auth code to token request endpoint and return access token
		"""
		request_params = [
			("grant_type", "authorization_code"),
			("code", code),
			("client_id", self.ClientId),
			("redirect_uri", redirect_uri or self.CallbackUrl)]
		if self.ClientSecret:
			request_params.append(("client_secret", self.ClientSecret))
		query_string = urllib.parse.urlencode(request_params)

		headers = {
			"content-type": "application/x-www-form-urlencoded"
		}
		async with aiohttp.ClientSession() as session:
			async with session.post(self.TokenEndpoint, data=query_string, headers=headers) as resp:
				if resp.status != 200:
					text = await resp.text()
					L.error("Error response from external auth provider.", struct_data={
						"status": resp.status,
						"url": resp.url,
						"text": text
					})
					raise ExternalOAuthFlowError("Token request failed.")
				else:
					yield resp

	async def get_user_info(self, authorize_data: dict, expected_nonce: str | None = None) -> typing.Optional[dict]:
		"""
		Obtain the authenticated user's profile info, with the claims normalized to be in line with
		OpenID UserInfo response.

		Supported claims:
		- sub (required)
		- preferred_username
		- email
		- phone_number
		- name
		- first_name
		- last_name
		"""
		code = authorize_data.get("code")
		if code is None:
			L.error("Code parameter not provided in authorize response.", struct_data={
				"provider": self.Type,
				"query": dict(authorize_data)})
			raise ExternalOAuthFlowError("No 'code' parameter in request.")

		async with self.token_request(code) as resp:
			token_data = await resp.json()

		if "id_token" not in token_data:
			L.error("Token response does not contain 'id_token'", struct_data={
				"provider": self.Type, "resp": token_data})
			raise ExternalOAuthFlowError("No 'id_token' in token response.")

		id_token = token_data["id_token"]
		await self._prepare_jwks()

		id_token_claims = self._get_verified_claims(id_token, expected_nonce)
		user_info = self._user_data_from_id_token_claims(id_token_claims)
		user_info["sub"] = str(user_info["sub"])
		return user_info

	def _user_data_from_id_token_claims(self, id_token_claims: dict):
		user_info = {
			k: v
			for k, v in id_token_claims.items()
			if k in {
				"iss", "sub", "email", "phone_number", "preferred_username", "name", "email_verified",
				"phone_number_verified", "nonce"
			} and v is not None
		}
		return user_info

	def _get_verified_claims(self, id_token, expected_nonce: str | None = None):
		check_claims = self._get_claims_to_verify()
		if expected_nonce:
			check_claims["nonce"] = expected_nonce
		try:
			id_token = jwcrypto.jwt.JWT(jwt=id_token, key=self.JwkSet, check_claims=check_claims)
			claims = json.loads(id_token.claims)
		except jwcrypto.jws.InvalidJWSSignature:
			L.error("Invalid ID token signature.", struct_data={"provider": self.Type})
			raise ExternalOAuthFlowError("Invalid ID token signature.")
		except jwcrypto.jwt.JWTExpired:
			L.error("Expired ID token.", struct_data={"provider": self.Type})
			raise ExternalOAuthFlowError("Expired ID token.")
		except Exception as e:
			L.error("Error reading ID token claims.", struct_data={
				"provider": self.Type, "error": str(e)})
			raise ExternalOAuthFlowError("Error reading ID token claims.")
		return claims


	def _get_claims_to_verify(self) -> dict:
		return {
			"iss": self.Issuer,
			"aud": self.ClientId
		}
