# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Logical ontology (v0).

Implements the spec at ``docs/ontology/ontology.md``: a logical,
backend-neutral ontology described in YAML, with entities, relationships,
properties, keys, and single-parent inheritance.
"""

from .binding_loader import load_binding
from .binding_loader import load_binding_from_string
from .binding_models import Backend
from .binding_models import BigQueryTarget
from .binding_models import Binding
from .binding_models import EntityBinding
from .binding_models import PropertyBinding
from .binding_models import RelationshipBinding
from .graph_ddl_compiler import compile_graph
from .ontology_loader import load_ontology
from .ontology_loader import load_ontology_from_string
from .ontology_models import Cardinality
from .ontology_models import Entity
from .ontology_models import Keys
from .ontology_models import Ontology
from .ontology_models import Property
from .ontology_models import PropertyType
from .ontology_models import Relationship
from .scaffold import scaffold

__all__ = [
    "Backend",
    "BigQueryTarget",
    "Binding",
    "Cardinality",
    "Entity",
    "EntityBinding",
    "Keys",
    "Ontology",
    "Property",
    "PropertyBinding",
    "PropertyType",
    "Relationship",
    "RelationshipBinding",
    "compile_graph",
    "load_binding",
    "load_binding_from_string",
    "load_ontology",
    "load_ontology_from_string",
    "scaffold",
]
