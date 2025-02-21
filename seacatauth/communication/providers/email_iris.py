import logging
import aiohttp
import asab
import asab.web.rest.json

from .abc import CommunicationProviderABC


L = logging.getLogger(__name__)


class AsabIrisEmailProvider(CommunicationProviderABC):

	Channel = "email"
	TemplateExtension = None
	ConfigDefaults = {
		"url": "http://localhost:8896",
		"template_path": "/Templates/Email/",
	}

	def __init__(self, app, config_section_name, config=None):
		super().__init__(app, config_section_name, config=config)
		self.AsabIrisUrl = self.Config.get("url").rstrip("/") + "/"
		self.TemplateBasePath = self.Config.get("template_path")


	def can_send_to_target(self, credentials: dict) -> bool:
		try:
			_get_email_address(credentials)
			return True
		except KeyError:
			return False


	async def build_message(self, credentials: dict, template_id: str, locale: str, **kwargs) -> dict:
		raise NotImplementedError()


	async def send_message(self, credentials: dict, message: dict, **kwargs):
		raise NotImplementedError()


	async def build_and_send_message(self, credentials: dict, template_id: str, locale: str, **kwargs):
		email_decl = {
			"to": [_get_email_address(credentials)],
			"body": {
				"template": self._get_template_path(template_id),
				"params": kwargs,
			}
		}
		data = asab.web.rest.json.JSONDumper(pretty=False)(email_decl)

		discovery_service = self.App.get_service("asab.DiscoveryService")
		if discovery_service is not None:
			open_session = discovery_service.session
		else:
			open_session = aiohttp.ClientSession

		url = "{}{}".format(self.AsabIrisUrl, "send_email")
		async with open_session() as session:
			async with session.put(url, data=data, headers={"Content-Type": "application/json"}) as resp:
				response = await resp.json()
				if resp.status == 200:
					L.log(asab.LOG_NOTICE, "Email sent.")
				else:
					L.error("Error response from ASAB Iris.", struct_data=response)
					raise RuntimeError("Email delivery failed.")


	def _get_template_path(self, template_id: str) -> str:
		templates = {
			"invitation": "Invitation.md",
			"new_user_password": "Credentials Created.md",
			"password_reset": "Password Reset.md",
		}
		return "{}{}".format(self.TemplateBasePath, templates[template_id])


def _get_email_address(credentials: dict) -> str:
	email = credentials.get("email")
	if not email:
		raise KeyError("Credentials do not contain 'email'.")
	return email
