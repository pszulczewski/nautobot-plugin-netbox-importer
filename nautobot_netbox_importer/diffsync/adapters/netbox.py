"""DiffSync adapters for NetBox data dumps."""

import json
from uuid import UUID, uuid4

import structlog

from .abstract import N2NDiffSync
from ..models.abstract import NautobotBaseModel
from ..models.validation import netbox_pk_to_nautobot_pk


class NetBox210DiffSync(N2NDiffSync):
    """DiffSync adapter for working with data from NetBox 2.10.x."""

    logger = structlog.get_logger()

    def __init__(self, *args, source_data=None, **kwargs):
        """Store the provided source_data for use when load() is called later."""
        self.source_data = source_data
        super().__init__(*args, **kwargs)

    def load_record(self, diffsync_model, record):  # pylint: disable=too-many-branches
        """Instantiate the given model class from the given record."""
        data = record["fields"].copy()
        data["pk"] = record["pk"]

        # Fixup fields that are actually foreign-key (FK) associations by replacing
        # their FK ids with the DiffSync model unique-id fields.
        for key, target_name in diffsync_model.fk_associations().items():
            if key not in data or not data[key]:
                # Null reference, no processing required.
                continue

            if target_name == "status":
                # Special case as Status is a hard-coded field in NetBox, not a model reference
                # Construct an appropriately-formatted mock natural key and use that instead
                # TODO: we could also do this with a custom validator on the StatusRef model; might be better?
                data[key] = {"slug": data[key]}
                continue

            # In the case of generic foreign keys, we have to actually check a different field
            # on the DiffSync model to determine the model type that this foreign key is referring to.
            # By convention, we label such fields with a '*', as if this were a C pointer.
            if target_name.startswith("*"):
                target_content_type_field = target_name[1:]
                target_content_type_pk = record["fields"][target_content_type_field]
                if not isinstance(target_content_type_pk, int):
                    self.logger.error(f"Invalid content-type PK value {target_content_type_pk}")
                    data[key] = None
                    continue
                target_content_type_record = self.get_by_pk(self.contenttype, target_content_type_pk)
                target_name = target_content_type_record.model

            # Identify the DiffSyncModel class that this FK is pointing to
            try:
                target_class = getattr(self, target_name)
            except AttributeError:
                self.logger.warning("Unknown/unrecognized class name!", name=target_name)
                data[key] = None
                continue

            if not issubclass(target_class, NautobotBaseModel):
                # A base Django model such as ContentType or Group.
                # Since we can't easily control its PK in Nautobot, use its actual natural key instead
                if isinstance(data[key], list):
                    data[key] = [self.get_by_pk(target_name, pk).get_identifiers() for pk in data[key]]
                else:
                    data[key] = self.get_by_pk(target_name, data[key]).get_identifiers()
                continue

            if isinstance(data[key], list):
                # This field is a one-to-many or many-to-many field, a list of foreign key references.
                # For each foreign key, find the corresponding DiffSync record, and use its
                # natural keys (identifiers) in the data in place of the foreign key value.
                data[key] = [self.get_fk_identifiers(diffsync_model, target_class, pk) for pk in data[key]]
            elif isinstance(data[key], (UUID, int)):
                if not issubclass(target_class, NautobotBaseModel):
                    # Look up the DiffSync record corresponding to this foreign key,
                    # and store its natural keys (identifiers) in the data in place of the foreign key value.
                    data[key] = self.get_fk_identifiers(diffsync_model, target_class, data[key])
                if isinstance(data[key], int):
                    data[key] = netbox_pk_to_nautobot_pk(target_name, data[key])
            else:
                self.logger.error(f"Invalid PK value {data[key]}")
                data[key] = None

        if diffsync_model == self.user:
            # NetBox has separate User and UserConfig models, but in Nautobot they're combined.
            # Load the corresponding UserConfig into the User record for completeness.
            self.logger.debug("Looking for UserConfig corresponding to User", username=data["username"])
            for other_record in self.source_data:
                if other_record["model"] == "users.userconfig" and other_record["fields"]["user"] == record["pk"]:
                    data["config_data"] = other_record["fields"]["data"]
                    break
            else:
                self.logger.warning("No UserConfig found for User", username=data["username"], pk=record["pk"])
                data["config_data"] = {}
        elif diffsync_model == self.customfield and data["type"] == "select":
            # NetBox stores the choices for a "select" CustomField (NetBox has no "multiselect" CustomFields)
            # locally within the CustomField model, whereas Nautobot has a separate CustomFieldChoices model.
            # So we need to split the choices out into separate DiffSync instances.
            # Since "choices" is an ArrayField, we have to parse it from the JSON string
            # see also models.abstract.ArrayField
            for choice in json.loads(data["choices"]):
                self.make_model(
                    self.customfieldchoice,
                    {"pk": uuid4(), "field": netbox_pk_to_nautobot_pk("customfield", record["pk"]), "value": choice},
                )
            del data["choices"]

        return self.make_model(diffsync_model, data)

    def load(self):
        """Load records from the provided source_data into DiffSync."""
        self.logger.info("Loading imported NetBox source data into DiffSync...")
        for modelname in ("contenttype", *self.top_level):
            diffsync_model = getattr(self, modelname)
            self.logger.info(f"Loading all {modelname} records...")
            content_type_label = diffsync_model.nautobot_model()._meta.label_lower
            # Handle a NetBox vs Nautobot discrepancy - the Nautobot target model is 'users.user',
            # but the NetBox data export will have user records under the label 'auth.user'.
            if content_type_label == "users.user":
                content_type_label = "auth.user"
            for record in self.source_data:
                if record["model"] == content_type_label:
                    self.load_record(diffsync_model, record)

        # TODO self.logger.info("Fixing up any previously unresolved object relations...")
        # TODO self.fixup_data_relations()

        self.logger.info("Data loading from NetBox source data complete.")
        # Discard the source data to free up memory
        self.source_data = None
