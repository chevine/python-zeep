from collections import OrderedDict

import six
from cached_property import threaded_cached_property

from zeep.xsd.elements import Element, Sequence
from zeep.xsd.utils import UniqueAttributeName
from zeep.xsd.valueobjects import CompoundValue


class Type(object):
    def __init__(self):
        self._resolved = False

    def accept(self, value):
        raise NotImplementedError

    def parse_xmlelement(self, xmlelement, schema=None):
        raise NotImplementedError

    def parsexml(self, xml, schema=None):
        raise NotImplementedError

    def render(self, parent, value):
        raise NotImplementedError

    def resolve(self):
        raise NotImplementedError

    @property
    def attributes(self):
        return []

    @classmethod
    def signature(cls):
        return ''


class UnresolvedType(Type):
    def __init__(self, qname, schema):
        self.qname = qname
        self.schema = schema

    def __repr__(self):
        return '<%s(qname=%r)>' % (self.__class__.__name__, self.qname)

    def resolve(self):
        retval = self.schema.get_type(self.qname)
        return retval.resolve()


class UnresolvedCustomType(Type):

    def __init__(self, name, base_qname, schema):
        assert name is not None
        self.name = name
        self.schema = schema
        self.base_qname = base_qname

    def resolve(self):
        base = self.base_qname
        if isinstance(self.base_qname, UnresolvedType):
            base = base.resolve()

        cls_attributes = {
            '__module__': 'zeep.xsd.dynamic_types',
        }
        xsd_type = type(self.name, (base.__class__,), cls_attributes)
        return xsd_type()


@six.python_2_unicode_compatible
class SimpleType(Type):
    name = None

    def __call__(self, *args, **kwargs):
        """Return the xmlvalue for the given value.

        The args, kwargs handling is done here manually so that we can return
        readable error messages instead of only '__call__ takes x arguments'

        """
        num_args = len(args) + len(kwargs)
        if num_args != 1:
            raise TypeError((
                '%s() takes exactly 1 argument (%d given). ' +
                'Simple types expect only a single value argument'
            ) % (self.__class__.__name__, num_args))

        if kwargs and 'value' not in kwargs:
            raise TypeError((
                '%s() got an unexpected keyword argument %r. ' +
                'Simple types expect only a single value argument'
            ) % (self.__class__.__name__, next(six.iterkeys(kwargs))))

        value = args[0] if args else kwargs['value']
        return self.xmlvalue(value)

    def __eq__(self, other):
        return (
            other is not None and
            self.__class__ == other.__class__ and
            self.__dict__ == other.__dict__)

    def __str__(self):
        return self.name

    def parse_xmlelement(self, xmlelement, schema=None):
        if xmlelement.text is None:
            return
        return self.pythonvalue(xmlelement.text)

    def pythonvalue(self, xmlvalue):
        raise NotImplementedError(
            '%s.pytonvalue() not implemented' % self.__class__.__name__)

    def render(self, parent, value):
        parent.text = self.xmlvalue(value)

    def resolve(self):
        return self

    def serialize(self, value):
        return value

    @classmethod
    def signature(cls):
        return cls.name

    def xmlvalue(self, value):
        raise NotImplementedError(
            '%s.xmlvalue() not implemented' % self.__class__.__name__)


class ComplexType(Type):
    name = None

    def __init__(self, element=None, attributes=None,
                 restriction=None, extension=None):
        if element and type(element) == list:
            element = Sequence(element)

        self._element = element
        self._attributes = attributes or []
        self._restriction = restriction
        self._extension = extension

        super(ComplexType, self).__init__()

    def __call__(self, *args, **kwargs):
        if not hasattr(self, '_value_class'):
            self._value_class = type(
                self.__class__.__name__, (CompoundValue,),
                {'_xsd_type': self, '__module__': 'zeep.objects'})

        return self._value_class(*args, **kwargs)

    def __str__(self):
        return '%s(%s)' % (self.__class__.__name__, self.signature())

    @property
    def name(self):
        return self.__class__.__name__

    @threaded_cached_property
    def attributes(self):
        result = []
        if self._extension:
            result.extend(self._extension.attributes)
        result.extend(self._attributes)
        return result

    @threaded_cached_property
    def elements(self):
        """List of tuples containing the element name and the element"""
        result = []
        for name, element in self.elements_nested:
            if isinstance(element, Element):
                result.append((element.name, element))
            else:
                result.extend(element.elements)
        return result

    @threaded_cached_property
    def elements_nested(self):
        """List of tuples containing the element name and the element"""
        result = []
        generator = UniqueAttributeName()

        if self._extension:
            name = generator.get_name()
            if isinstance(self._extension, SimpleType):
                result.append((name, Element(name, self._extension)))
            else:
                result.extend(self._extension.elements_nested)
        # _element is one of All, Choice, Group, Sequence
        if self._element:
            result.append((generator.get_name(), self._element))
        return result

    def parse_xmlelement(self, xmlelement, schema):
        init_kwargs = {}

        elements = xmlelement.getchildren()
        attributes = xmlelement.attrib
        if not elements and not attributes:
            return None  # object is nil

        # Parse attributes
        attr_map = {attr.name: attr for attr in self.attributes}
        for key, value in attributes.items():
            attr = attr_map.get(key)
            if not attr:
                continue
            value = attr.parse(value, schema)
            init_kwargs[key] = value

        # Parse elements
        children = xmlelement.getchildren()
        for name, element in self.elements_nested:
            result = element.parse_xmlelements(children, schema, name)
            init_kwargs.update(result)

        return self(**init_kwargs)

    def render(self, parent, value, xsd_type=None):
        for attribute in self.attributes:
            attr_value = getattr(value, attribute.name, None)
            attribute.render(parent, attr_value)

        for name, element in self.elements_nested:
            if isinstance(element, Element):
                element.type.render(parent, getattr(value, name))
            else:
                element.render(parent, value)

        if xsd_type:
            parent.set(
                '{http://www.w3.org/2001/XMLSchema-instance}type',
                xsd_type._xsd_name)

    def resolve(self):
        """ EXTENDS / RESTRICTS """
        if self._resolved:
            return self
        self._resolved = True

        if self._extension:
            self._extension = self._extension.resolve()

        if self._restriction:
            self._restriction = self._restriction.resolve()

        if self._element:
            self._element = self._element.resolve()

        for i, attribute in enumerate(self._attributes):
            self._attributes[i] = attribute.resolve()
            assert self._attributes[i] is not None
        return self

    def serialize(self, value):
        result = OrderedDict()

        for name, element in self.elements_nested:
            if isinstance(element, list):
                for subfield in element:
                    field_value = getattr(value, subfield.name, None)
                    result[subfield.name] = subfield.serialize(field_value)
            else:
                field_value = getattr(value, element.name, None)
                result[element.name] = element.serialize(field_value)
        return result

    def signature(self):
        parts = []
        for name, element in self.elements_nested:

            # http://schemas.xmlsoap.org/soap/encoding/ contains cyclic type
            if isinstance(element, Element) and element.type == self:
                continue

            part = element.signature()
            parts.append(part)

        for attribute in self.attributes:
            part = attribute.signature()
            parts.append(part)

        return ', '.join(parts)


class ListType(Type):
    def __init__(self, item_type):
        self.item_type = item_type

    def render(self, parent, value):
        parent.text = self.xmlvalue(value)

    def resolve(self):
        self.item_type = self.item_type.resolve()
        return self

    def xmlvalue(self, value):
        item_type = self.item_type
        return ' '.join(item_type.xmlvalue(v) for v in value)


class UnionType(object):
    def __init__(self, item_types):
        self.item_types = item_types

    def resolve(self):
        self.item_types = [item.resolve() for item in self.item_types]
        return self

    def signature(self):
        return ''
