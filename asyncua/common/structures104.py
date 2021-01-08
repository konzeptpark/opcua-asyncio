from enum import Enum
from datetime import datetime
import uuid
from enum import IntEnum
import logging
import re

from asyncua import ua
from asyncua import Node
from asyncua.common.manage_nodes import create_encoding, create_data_type

logger = logging.getLogger(__name__)


def new_struct_field(name, dtype, array=False, optional=False, description=""):
    """
    simple way to create a StructureField
    """
    field = ua.StructureField()
    field.Name = name
    field.IsOptional = optional
    if description:
        field.Description = ua.LocalizedText(text=description)
    else:
        field.Description = ua.LocalizedText(text=name)
    if isinstance(dtype, ua.VariantType):
        field.DataType = ua.NodeId(dtype.value, 0)
    elif isinstance(dtype, ua.NodeId):
        field.DataType = dtype
    elif isinstance(dtype, Node):
        field.DataType = dtype.nodeid
    else:
        raise ValueError(f"DataType of a field must be a NodeId, not {dtype} of type {type(dtype)}")
    if array:
        field.ValueRank = ua.ValueRank.OneOrMoreDimensions
        field.ArrayDimensions = [1]
    else:
        field.ValueRank = ua.ValueRank.Scalar
        field.ArrayDimensions = []
    return field


async def new_struct(server, idx, name, fields):
    """
    simple way to create a new structure
    return the created data type node and the list of encoding nodes
    """
    dtype = await create_data_type(server.nodes.base_structure_type, idx, name)
    enc = await create_encoding(dtype, idx, "Default Binary")
    # TODO: add other encoding the day we support them

    sdef = ua.StructureDefinition()
    sdef.StructureType = ua.StructureType.Structure
    for field in fields:
        if field.IsOptional:
            sdef.StructureType = ua.StructureType.StructureWithOptionalFields
            break
    sdef.Fields = fields
    sdef.BaseDataType = server.nodes.base_data_type.nodeid
    sdef.DefaultEncodingId = enc.nodeid

    await dtype.write_data_type_definition(sdef)
    return dtype, [enc]


async def new_enum(server, idx, name, values):
    edef = ua.EnumDefinition()
    counter = 0
    for val_name in values:
        field = ua.EnumField()
        field.DisplayName = ua.LocalizedText(text=val_name)
        field.Name = val_name
        field.Value = counter
        counter += 1
        edef.Fields.append(field)

    dtype = await server.nodes.enum_data_type.add_data_type(idx, name)
    await dtype.write_data_type_definition(edef)
    return dtype


def clean_name(name):
    """
    Remove characters that might be present in  OPC UA structures
    but cannot be part of of Python class names
    """
    newname = re.sub(r'\W+', '_', name)
    newname = re.sub(r'^[0-9]+', r'_\g<0>', newname)

    if name != newname:
        logger.warning("renamed %s to %s due to Python syntax", name, newname)
    return newname


def get_default_value(uatype, enums=None):
    if enums is None:
        enums = {}
    if uatype == "String":
        return "None"
    elif uatype == "Guid":
        return "uuid.uuid4()"
    elif uatype in ("ByteString", "CharArray", "Char"):
        return b''
    elif uatype == "Boolean":
        return "True"
    elif uatype == "DateTime":
        return "datetime.utcnow()"
    elif uatype in ("Int16", "Int32", "Int64", "UInt16", "UInt32", "UInt64", "Double", "Float", "Byte", "SByte"):
        return 0
    elif uatype in enums:
        return f"ua.{uatype}({enums[uatype]})"
    elif hasattr(ua, uatype) and issubclass(getattr(ua, uatype), Enum):
        # We have an enum, try to initilize it correctly
        val = list(getattr(ua, uatype).__members__)[0]
        return f"ua.{uatype}.{val}"
    else:
        return f"ua.{uatype}()"


def make_structure_code(data_type, struct_name, sdef):
    """
    given a StructureDefinition object, generate Python code
    """
    if sdef.StructureType not in (ua.StructureType.Structure, ua.StructureType.StructureWithOptionalFields):
        # if sdef.StructureType != ua.StructureType.Structure:
        raise NotImplementedError(f"Only StructureType implemented, not {ua.StructureType(sdef.StructureType).name} for node {struct_name} with DataTypdeDefinition {sdef}")

    code = f"""

class {struct_name}:

    '''
    {struct_name} structure autogenerated from StructureDefinition object
    '''

    data_type = ua.NodeId({data_type.Identifier}, {data_type.NamespaceIndex})

"""
    counter = 0
    # FIXME: to support inheritance we probably need to add all fields from parents
    # this requires network call etc...
    if sdef.StructureType == ua.StructureType.StructureWithOptionalFields:
        code += '    ua_switches = {\n'
        for field in sdef.Fields:
            fname = clean_name(field.Name)

            if field.IsOptional:
                code += f"        '{fname}': ('Encoding', {counter}),\n"
                counter += 1
        code += "    }\n\n"

    code += '    ua_types = [\n'
    if sdef.StructureType == ua.StructureType.StructureWithOptionalFields:
        code += "        ('Encoding', 'Byte'),\n"
    uatypes = []
    for field in sdef.Fields:
        fname = clean_name(field.Name)
        prefix = ""
        if field.ValueRank >= 1 or field.ArrayDimensions:
            prefix = 'ListOf'
        if field.DataType.NamespaceIndex == 0 and field.DataType.Identifier in ua.ObjectIdNames:
            uatype = ua.ObjectIdNames[field.DataType.Identifier]
        elif field.DataType in ua.extension_objects_by_datatype:
            uatype = ua.extension_objects_by_datatype[field.DataType].__name__
        elif field.DataType in ua.enums_by_datatype:
            uatype = ua.enums_by_datatype[field.DataType].__name__
        else:
            # FIXME: we are probably missing many custom tyes here based on builtin types
            # maybe we can use ua_utils.get_base_data_type()
            raise RuntimeError(f"Unknown datatype for field: {field} in structure:{struct_name}, please report")
        if field.ValueRank >= 1 and uatype == 'Char':
            uatype = 'String'
        uatypes.append((field, uatype))
        code += f"        ('{fname}', '{prefix + uatype}'),\n"
    code += "    ]\n"
    code += f"""
    def __str__(self):
        vals = [f"{{field_name}}:{{val}}" for field_name, val in self.__dict__.items()]
        return f"{struct_name}({{','.join(vals)}})"

    __repr__ = __str__

    def __init__(self):
"""
    if not sdef.Fields:
        code += "      pass"
    if sdef.StructureType == ua.StructureType.StructureWithOptionalFields:
        code += "        self.Encoding = 0\n"
    for field, uatype in uatypes:
        fname = clean_name(field.Name)
        if field.ValueRank >= 1:
            default_value = "[]"
        else:
            default_value = get_default_value(uatype)
        code += f"        self.{fname} = {default_value}\n"
    return code


async def _generate_object(name, sdef, data_type=None, env=None, enum=False):
    """
    generate Python code and execute in a new environment
    return a dict of structures {name: class}
    Rmw: Since the code is generated on the fly, in case of error the stack trace is
    not available and debugging is very hard...
    """
    if env is None:
        env = {}
    #  Add the required libraries to dict
    if "ua" not in env:
        env['ua'] = ua
    if "datetime" not in env:
        env['datetime'] = datetime
    if "uuid" not in env:
        env['uuid'] = uuid
    if "enum" not in env:
        env['IntEnum'] = IntEnum
    # generate classe add it to env dict
    if enum:
        code = make_enum_code(name, sdef)
    else:
        code = make_structure_code(data_type, name, sdef)
    logger.debug("Executing code: %s", code)
    exec(code, env)
    return env


class DataTypeSorter:
    def __init__(self, data_type, name, desc, sdef):
        self.data_type = data_type
        self.name = name
        self.desc = desc
        self.sdef = sdef
        self.encoding_id = self.sdef.DefaultEncodingId
        self.deps = [field.DataType for field in self.sdef.Fields]

    def __lt__(self, other):
        if self.desc.NodeId in other.deps:
            return True
        return False

    def __str__(self):
        return f"{self.__class__.__name__}({self.desc.NodeId, self.deps, self.encoding_id})"

    __repr__ = __str__


async def _recursive_parse(server, base_node, dtypes, parent_sdef=None):
    for desc in await base_node.get_children_descriptions(refs=ua.ObjectIds.HasSubtype):
        sdef = await _read_data_type_definition(server, desc)
        if not sdef:
            continue
        name = clean_name(desc.BrowseName.Name)
        if parent_sdef:
            for field in reversed(parent_sdef.Fields):
                sdef.Fields.insert(0, field)
        dtypes.append(DataTypeSorter(desc.NodeId, name, desc, sdef))
        await _recursive_parse(server, server.get_node(desc.NodeId), dtypes, parent_sdef=sdef)


async def load_data_type_definitions(server, base_node=None):
    await load_enums(server)  # we need all enums to generate structure code
    if base_node is None:
        base_node = server.nodes.base_structure_type
    dtypes = []
    await _recursive_parse(server, base_node, dtypes)
    dtypes.sort()
    for dts in dtypes:
        try:
            env = await _generate_object(dts.name, dts.sdef, data_type=dts.data_type)
            ua.register_extension_object(dts.name, dts.encoding_id, env[dts.name], dts.desc.NodeId)
        except NotImplementedError:
            logger.exception("Structure type %s not implemented", dts.sdef)


async def _read_data_type_definition(server, desc):
    if desc.BrowseName.Name == "FilterOperand":
        # FIXME: find out why that one is not in ua namespace...
        return None
    # FIXME: this is fishy, we may have same name in different Namespaces
    if hasattr(ua, desc.BrowseName.Name):
        return None
    logger.warning("Registring data type %s %s", desc.NodeId, desc.BrowseName)
    node = server.get_node(desc.NodeId)
    try:
        sdef = await node.read_data_type_definition()
    except ua.uaerrors.BadAttributeIdInvalid:
        logger.warning("%s has no DataTypeDefinition atttribute", node)
        return None
    except Exception:
        logger.exception("Error getting datatype for node %s", node)
        return None
    return sdef


def make_enum_code(name, edef):
    """
    if node has a DataTypeDefinition arttribute, generate enum code
    """
    code = f"""

class {name}(IntEnum):

    '''
    {name} EnumInt autogenerated from EnumDefinition
    '''

"""

    for field in edef.Fields:
        name = clean_name(field.Name)
        value = field.Value
        code += f"    {name} = {value}\n"

    return code


async def load_enums(server, base_node=None):
    if base_node is None:
        base_node = server.nodes.enum_data_type
    for desc in await base_node.get_children_descriptions(refs=ua.ObjectIds.HasSubtype):
        name = clean_name(desc.BrowseName.Name)
        if hasattr(ua, name):
            continue
        logger.warning("Registring Enum %s %s", desc.NodeId, name)
        edef = await _read_data_type_definition(server, desc)
        if not edef:
            continue
        env = await _generate_object(name, edef, enum=True)
        ua.register_enum(name, desc.NodeId, env[name])
