class TenantNotSpecified(Exception):
	pass


class AuthenticationRequired(Exception):
	"""
	Accessing this resource (or tenant, operation...) requires that the subject be authenticated.

	Equivalent to HTTP 401 Unauthorized.
	"""
	pass


class AccessDenied(Exception):
	"""
	Subject is not authorized to access requested resource (or tenant, operation...).

	Equivalent to HTTP 403 Forbidden.
	"""
	def __init__(self, message=None, *args, subject=None, resource=None):
		self.Subject = subject
		self.Resource = resource
		if message is not None:
			super().__init__(message, *args)
		elif resource is not None:
			if subject is not None:
				message = "Subject {!r} is not authorized to access {!r}.".format(subject, resource)
			else:
				message = "Not authorized to access {!r}.".format(resource)
			super().__init__(message, *args)
		else:
			super().__init__(*args)


class TenantAccessDenied(AccessDenied):
	"""
	Subject is not authorized to access requested tenant.
	"""
	def __init__(self, tenant, subject=None):
		self.Tenant = tenant
		super().__init__(subject=subject, resource=tenant)


class NoTenants(AccessDenied):
	"""
	Subject has access to no tenants.
	"""
	def __init__(self, subject=None, *args):
		self.Subject = subject
		if subject is not None:
			message = "Subject {!r} has access to no tenant.".format(subject)
		else:
			message = "Subject has access to no tenant."
		super().__init__(message, *args)


class TenantNotFound(KeyError):
	def __init__(self, tenant, *args):
		self.Tenant = tenant
		super().__init__("Tenant {!r} not found.".format(tenant), *args)
