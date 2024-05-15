import re
import typing

from ....events import EventTypes
from .abc import RoleProvider


class SharedRoleProvider(RoleProvider):
	def __init__(self, storage_service, collection_name, tenant_id):
		super().__init__(storage_service, collection_name)
		self.TenantId = tenant_id


	def _build_query(
		self,
		name_filter: typing.Optional[str] = None,
		resource_filter: typing.Optional[str] = None,
		**kwargs
	):
		query = {"tenant": None, "shared": True}
		if name_filter:
			query["_id"] = {"$regex": re.escape(name_filter)}
		if resource_filter:
			query["resources"] = resource_filter
		return query


	async def count(
		self,
		name_filter: typing.Optional[str] = None,
		resource_filter: typing.Optional[str] = None,
		**kwargs
	) -> int | None:
		query = self._build_query(name_filter=name_filter, resource_filter=resource_filter)
		return await self.StorageService.Database[self.CollectionName].count_documents(query)


	async def iterate(
		self,
		offset: int = 0,
		limit: typing.Optional[int] = None,
		sort: typing.Tuple[str, int] = ("_id", 1),
		name_filter: typing.Optional[str] = None,
		resource_filter: typing.Optional[str] = None,
		**kwargs
	) -> typing.AsyncGenerator:
		query = self._build_query(name_filter=name_filter, resource_filter=resource_filter)
		async for role in self._iterate(offset, limit, query, sort):
			yield role


	async def get(self, role_id: str) -> dict:
		assert self.role_tenant_matches(role_id)
		return await self.StorageService.get(self.CollectionName, self._tenant_id_to_global(role_id))


	def role_tenant_matches(self, role_id: str):
		return role_id.split("/")[0] == self.TenantId


	def _global_id_to_tenant(self, role_id: str):
		return self.TenantId + role_id.split("/")[1]


	def _tenant_id_to_global(self, role_id: str):
		return "*" + role_id.split("/")[1]


	def _project_role_into_tenant(self, role: dict):
		role["global_id"] = role["_id"]
		role["_id"] = self._global_id_to_tenant(role["_id"])
		role["editable"] = False
		return role
