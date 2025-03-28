import typing
import logging
import datetime
import pyotp
import urllib.parse
import asab
import asab.storage

from .. import exceptions
from ..events import EventTypes


L = logging.getLogger(__name__)


class OTPService(asab.Service):
	PreparedTOTPCollection = "tos"
	TOTPCollection = "totp"

	def __init__(self, app, service_name="seacatauth.OTPService"):
		super().__init__(app, service_name)
		self.StorageService = app.get_service("asab.StorageService")
		self.CredentialsService = app.get_service("seacatauth.CredentialsService")
		self.Issuer = asab.Config.get("seacatauth:otp", "issuer")
		if len(self.Issuer) == 0:
			auth_webui_base_url = app.AuthWebUiUrl.rstrip("/")
			self.Issuer = str(urllib.parse.urlparse(auth_webui_base_url).hostname)
		self.RegistrationTimeout = datetime.timedelta(
			seconds=asab.Config.getseconds("seacatauth:otp", "registration_timeout")
		)

		app.PubSub.subscribe("Application.housekeeping!", self._on_housekeeping)


	async def _on_housekeeping(self, event_name):
		await self._delete_expired_totp_secrets()


	async def deactivate_totp(self, credentials_id: str):
		"""
		Delete active TOTP secret for requested credentials.
		"""
		if not await self.has_activated_totp(credentials_id):
			L.log(asab.LOG_NOTICE, "Cannot deactivate TOTP because it is already active.", struct_data={
				"cid": credentials_id})
			raise exceptions.TOTPDeactivationError("TOTP is not active.", credentials_id)
		try:
			await self.StorageService.delete(collection=self.TOTPCollection, obj_id=credentials_id)
		except KeyError:
			# TOTP are stored in the old self.PreparedTOTPCollection -> only for backward compatibility
			pass


		provider = self.CredentialsService.get_provider(credentials_id)
		await provider.update(credentials_id, {
			"__totp": None
		})


	async def prepare_totp(self, session, credentials_id: str) -> dict:
		"""
		Prepare TOTP specifications from credentials and session.
		"""

		credentials: dict = await self.CredentialsService.get(credentials_id)
		secret: str = await self._create_totp_secret(session.SessionId)
		username: str = credentials.get("username")
		url: str = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=self.Issuer)

		totp_setup: dict = {
			"url": url,
			"username": username,
			"issuer": self.Issuer,
			"secret": secret,
			"timeout": self.RegistrationTimeout.total_seconds(),
		}

		return totp_setup

	async def activate_prepared_totp(self, session, credentials_id: str, request_otp: str):
		"""
		Activate TOTP for the current user, provided that a TOTP secret is already set.
		Requires entering the generated OTP to succeed.
		"""
		if await self.has_activated_totp(credentials_id):
			L.log(asab.LOG_NOTICE, "Cannot activate TOTP because it is already active.", struct_data={
				"cid": credentials_id})
			raise exceptions.TOTPActivationError("TOTP is already active.", credentials_id)

		try:
			secret = await self._get_prepared_totp_secret_by_session_id(session.SessionId)
		except KeyError:
			L.log(asab.LOG_NOTICE, "Cannot activate TOTP because the secret is not ready or has expired.", struct_data={
				"cid": credentials_id})
			raise exceptions.TOTPActivationError("TOTP secret is not ready, or possibly has expired.", credentials_id)

		totp = pyotp.TOTP(secret)
		if totp.verify(request_otp) is False:
			# TOTP secret does not match
			L.log(asab.LOG_NOTICE, "Cannot activate TOTP because the verification failed.", struct_data={
				"cid": credentials_id})
			raise exceptions.TOTPActivationError("TOTP verification failed.", credentials_id)

		# Store secret in its own dedicated collection
		upsertor = self.StorageService.upsertor(collection=self.TOTPCollection, obj_id=credentials_id)
		upsertor.set("__totp", secret.encode("ascii"), encrypt=True)
		await upsertor.execute(event_type=EventTypes.TOTP_REGISTERED)
		L.log(asab.LOG_NOTICE, "TOTP activated.", struct_data={"cid": credentials_id})

		await self._delete_prepared_totp_secret(session.SessionId)


	async def _create_totp_secret(self, session_id: str) -> str:
		"""
		Create TOTP secret and save it into `PreparedTOTPCollection`. Delete it if already exists.
		"""
		# Delete secret if exists.
		try:
			await self._delete_prepared_totp_secret(session_id)
		except KeyError:
			# There is no secret associated with this user session
			pass

		# Store expiration date and secret to PreparedTOTPCollection
		upsertor = self.StorageService.upsertor(collection=self.PreparedTOTPCollection, obj_id=session_id)
		expires: datetime.datetime = datetime.datetime.now(datetime.timezone.utc) + self.RegistrationTimeout
		upsertor.set("exp", expires)

		secret = pyotp.random_base32().encode("ascii")
		upsertor.set("__s", secret, encrypt=True)

		await upsertor.execute(event_type=EventTypes.TOTP_CREATED)
		L.log(asab.LOG_NOTICE, "TOTP secret created.", struct_data={"sid": session_id})

		return secret


	async def _get_prepared_totp_secret_by_session_id(self, session_id: str) -> str:
		"""
		Get TOTP secret from `PreparedTOTPCollection`. If it has already expired, raise `KeyError`.
		"""
		data: dict = await self.StorageService.get(collection=self.PreparedTOTPCollection, obj_id=session_id, decrypt=["__s"])
		secret: bytes = data["__s"]
		expiration_time: typing.Optional[datetime.datetime] = data["exp"]
		if expiration_time is None or expiration_time < datetime.datetime.now(datetime.timezone.utc):
			raise KeyError("TOTP secret timed out")

		return secret.decode("ascii")

	async def _get_totp_secret_by_credentials_id(self, credentials_id: str) -> typing.Optional[str]:
		"""
		Get TOTP secret from `TOTPCollection` by `credentials_id`.
		Backwards compatibility: if not found, get TOTP from `CredentialsService`.
		"""
		try:
			totp_object: dict = await self.StorageService.get(collection=self.TOTPCollection, obj_id=credentials_id, decrypt=["__totp"])
			secret: None | bytes = totp_object.get("__totp")
		except KeyError:
			secret = None

		if secret is not None:
			return secret.decode("ascii")

		credentials: dict = await self.CredentialsService.get(credentials_id, include=frozenset(["__totp"]))
		secret = credentials.get("__totp")

		return secret

	async def _delete_prepared_totp_secret(self, session_id: str):
		"""
		Delete TOTP secret from `PreparedTOTPCollection`.
		"""
		await self.StorageService.delete(collection=self.PreparedTOTPCollection, obj_id=session_id)
		L.info("TOTP secret deleted.", struct_data={"sid": session_id})


	async def _delete_expired_totp_secrets(self):
		"""
		Delete expired TOTP secrets
		"""
		collection = self.StorageService.Database[self.PreparedTOTPCollection]
		query_filter: dict = {"exp": {"$lt": datetime.datetime.now(datetime.timezone.utc)}}
		result = await collection.delete_many(query_filter)
		if result.deleted_count > 0:
			L.log(asab.LOG_NOTICE, "Expired TOTP secrets deleted.", struct_data={
				"count": result.deleted_count
			})

	async def has_activated_totp(self, credentials_id: str) -> bool:
		"""
		Check if the user has TOTP activated from TOTPCollection. (For backward compatibility: check also PreparedTOTPCollection.)
		"""
		secret: typing.Optional[str] = await self._get_totp_secret_by_credentials_id(credentials_id)
		if secret is not None and len(secret) > 0:
			return True
		return False


	async def verify_request_totp(self, credentials_id, request_data: dict) -> bool:
		totp_secret: typing.Optional[str] = await self._get_totp_secret_by_credentials_id(credentials_id)

		try:
			totp_object: pyotp.TOTP = pyotp.TOTP(totp_secret)
			return totp_object.verify(request_data['totp'])
		except KeyError:
			return False
