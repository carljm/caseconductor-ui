"""
Product-related remote objects.

"""
from ..core.api import RemoteObject, ListObject, fields
from ..core.models import Company


class Product(RemoteObject):
    company = fields.Locator(Company)
    description = fields.Field()
    name = fields.Field()


    def __unicode__(self):
        return self.name


class ProductList(ListObject):
    entryclass = Product
    api_name = "products"
    default_url = "products"

    entries = fields.List(fields.Object(Product))


    def __unicode__(self):
        return u"%s Products" % len(self)
