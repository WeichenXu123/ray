# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: src/ray/protobuf/dependency.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()




DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n!src/ray/protobuf/dependency.proto\x12\x07ray.rpc\"\x1d\n\x0ePythonFunction\x12\x0b\n\x03key\x18\x01 \x01(\x0c\x42\x03\xf8\x01\x01\x62\x06proto3')



_PYTHONFUNCTION = DESCRIPTOR.message_types_by_name['PythonFunction']
PythonFunction = _reflection.GeneratedProtocolMessageType('PythonFunction', (_message.Message,), {
  'DESCRIPTOR' : _PYTHONFUNCTION,
  '__module__' : 'src.ray.protobuf.dependency_pb2'
  # @@protoc_insertion_point(class_scope:ray.rpc.PythonFunction)
  })
_sym_db.RegisterMessage(PythonFunction)

if _descriptor._USE_C_DESCRIPTORS == False:

  DESCRIPTOR._options = None
  DESCRIPTOR._serialized_options = b'\370\001\001'
  _PYTHONFUNCTION._serialized_start=46
  _PYTHONFUNCTION._serialized_end=75
# @@protoc_insertion_point(module_scope)
