"""MySQL target sink class, which handles writing streams."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterable

import sqlalchemy
from pendulum import now
from singer_sdk.sinks import SQLSink
from sqlalchemy import Column, MetaData, Table, insert, select, update
from sqlalchemy.sql.expression import bindparam

from target_mysql.connector import MySQLConnector

if TYPE_CHECKING:
    from singer_sdk.target_base import Target
    from sqlalchemy.sql import Executable


class MySQLSink(SQLSink):
    """MySQL target sink class."""

    connector_class = MySQLConnector
    MAX_SIZE_DEFAULT = 100000


    def __init__(self, target: Target, *args: tuple, **kwargs: dict) -> None:
        """Initialize SQL Sink. See super class for more details."""
        connector = MySQLConnector(config=dict(target.config))
        super().__init__(
            target=target,
            connector=connector,
            *args,  # noqa: B026
            **kwargs,
        )
        self.temp_table_name = self.generate_temp_table_name()

    @property
    def append_only(self) -> bool:
        """Return True if the target is append only."""
        return self._append_only

    @append_only.setter
    def append_only(self, value: bool) -> None:
        """Set the append_only attribute."""
        self._append_only = value

    def setup(self) -> None:
        """Set up Sink.

        This method is called on Sink creation, and creates the required Schema and
        Table entities in the target database.
        """
        if self.key_properties is None or self.key_properties == []:
            self.append_only = True
        else:
            self.append_only = False
        if self.schema_name:
            self.connector.prepare_schema(self.schema_name)
        self.connector.prepare_table(
            full_table_name=self.full_table_name,
            schema=self.schema,
            primary_keys=self.key_properties,
            as_temp_table=False,
        )

    def process_batch(self, context: dict) -> None:
        """Process a batch with the given batch context.

        Writes a batch to the SQL target. Developers may override this method
        in order to provide a more efficient upload/upsert process.

        Args:
            context: Stream partition or context dictionary.
        """
        # First we need to be sure the main table is already created
        table: sqlalchemy.Table = self.connector.prepare_table(
            full_table_name=self.full_table_name,
            schema=self.schema,
            primary_keys=self.key_properties,
            as_temp_table=False,
        )
        # Create a temp table (Creates from the table above)
        temp_table: sqlalchemy.Table = self.connector.prepare_table(
            full_table_name=self.temp_table_name,
            schema=self.schema,
            primary_keys=self.key_properties,
            as_temp_table=True,
        )
        # Insert into temp table
        self.bulk_insert_records(
            table=temp_table,
            schema=self.schema,
            primary_keys=self.key_properties,
            records=context["records"],
        )
        # Merge data from Temp table to main table
        self.upsert(
            from_table=temp_table,
            to_table=table,
            schema=self.schema,
            join_keys=self.key_properties,
        )
        # Drop temp table
        self.connector.drop_table(temp_table)

    def generate_temp_table_name(self) -> str:
        """Uuid temp table name."""
        # 'temp_test_optional_attributes_388470e9_fbd0_47b7_a52f_d32a2ee3f5f6'
        # exceeds maximum length of 63 characters
        # Is hit if we have a long table name, there is no limit on Temporary tables
        # in MySQL, used a guid just in case we are using the same session
        return f"{str(uuid.uuid4()).replace('-','_')}"

    def bulk_insert_records(
        self,
        table: sqlalchemy.Table,
        schema: dict,
        records: Iterable[dict[str, Any]],
        primary_keys: list[str],
    ) -> int | None:
        """Bulk insert records to an existing destination table.

        The default implementation uses a generic SQLAlchemy bulk insert operation.
        This method may optionally be overridden by developers in order to provide
        faster, native bulk uploads.

        Args:
            full_table_name: the target table name.
            schema: the JSON schema for the new table, to be used when inferring column
                names.
            records: the input records.
            table: the table to insert records into.
            primary_keys: the primary keys for the table to insert records into.

        Returns:
            True if table exists, False if not, None if unsure or undetectable.
        """
        columns = self.column_representation(schema)
        insert = self.generate_insert_statement(
            table.name,
            columns,
        )
        self.logger.info("Inserting with SQL: %s", insert)
        # Only one record per PK, we want to take the last one
        data_to_insert: list[dict[str, Any]] = []

        if self.append_only is False:
            insert_records: dict[str, dict] = {}  # pk : record
            try:
                for record in records:
                    insert_record = {}
                    for column in columns:
                        insert_record[column.name] = record.get(column.name)
                    primary_key_value = "".join(
                        [str(record[key]) for key in primary_keys],
                    )
                    insert_records[primary_key_value] = insert_record
            except KeyError as e:
                msg = (
                    f"Primary key not found in record. full_table_name: {table.name}. "
                    f"schema: {table.schema}.  primary_keys: {primary_keys}."
                )
                raise RuntimeError(msg) from e
            data_to_insert = list(insert_records.values())
        else:
            for record in records:
                insert_record = {}
                for column in columns:
                    if isinstance(record.get(column.name), (dict, list, Decimal)):
                        # Necessary because Decimals aren't correctly serialized into
                        # into json when present in data_to_insert
                        # Is this the only place records might need sanitization?
                        insert_record[column.name] = self.sanitize_entry(
                            record.get(column.name),
                        )
                        continue
                    insert_record[column.name] = record.get(column.name)
                data_to_insert.append(insert_record)
        self.connector.connection.execute(insert.prefix_with('IGNORE'), data_to_insert)
        return True

    def sanitize_entry(self, to_sanitize: Any) -> dict | list | str:  # noqa: ANN401
        """Remove all Decimal objects and converts them to strings.

        Allows json serialization to work correctly.

        Args:
            to_sanitize: An object to sanitize by removing Decimal objects.

        Returns:
            A sanitized version of the provided object, without Decimal objects.
        """
        if isinstance(to_sanitize, dict):
            return {k: self.sanitize_entry(v) for (k, v) in to_sanitize.items()}
        if isinstance(to_sanitize, list):
            return [self.sanitize_entry(i) for i in to_sanitize]
        if isinstance(to_sanitize, Decimal):
            return str(to_sanitize)
        return to_sanitize

    def upsert(
        self,
        from_table: sqlalchemy.Table,
        to_table: sqlalchemy.Table,
        schema: dict,  # noqa: ARG002
        join_keys: list[Column],
    ) -> int | None:
        """Merge upsert data from one table to another.

        Args:
            from_table: The source table name.
            to_table: The destination table name.
            join_keys: The merge upsert keys, or `None` to append.
            schema: Singer Schema message.

        Return:
            The number of records copied, if detectable, or `None` if the API does not
            report number of records affected/inserted.

        """
        if self.append_only is True:
            # Insert
            select_stmt = select(from_table.columns).select_from(from_table)
            insert_stmt = to_table.insert().from_select(
                names=from_table.columns,
                select=select_stmt,
            )
            self.connection.execute(insert_stmt)
        else:
            join_predicates = []
            for key in join_keys:
                from_table_key: sqlalchemy.Column = from_table.columns[key]
                to_table_key: sqlalchemy.Column = to_table.columns[key]
                join_predicates.append(from_table_key == to_table_key)

            join_condition = sqlalchemy.and_(*join_predicates)

            where_predicates = []
            for key in join_keys:
                to_table_key: sqlalchemy.Column = to_table.columns[key]
                where_predicates.append(to_table_key.is_(None))
            where_condition = sqlalchemy.and_(*where_predicates)

            select_stmt = (
                select(from_table.columns)
                .select_from(from_table.outerjoin(to_table, join_condition))
                .where(where_condition)
            )
            insert_stmt = insert(to_table).from_select(
                names=from_table.columns,
                select=select_stmt,
            )
            self.connection.execute(insert_stmt)

            # Update
            where_condition = join_condition
            update_columns = {}
            for column_name in self.schema["properties"]:
                from_table_column: sqlalchemy.Column = from_table.columns[column_name]
                to_table_column: sqlalchemy.Column = to_table.columns[column_name]
                update_columns[to_table_column] = from_table_column

            update_stmt = update(to_table).where(where_condition).values(update_columns)
            self.connection.execute(update_stmt)

    def column_representation(
        self,
        schema: dict,
    ) -> list[Column]:
        """Return a sqlalchemy table representation for the current schema."""
        columns: list[Column] = []
        for property_name, property_jsonschema in schema["properties"].items():
            columns.append(
                Column(
                    property_name,
                    self.connector.to_sql_type(
                        property_jsonschema,
                        self.config["max_varchar_size"],
                    ),
                ),
            )
        return columns

    def generate_insert_statement(
        self,
        full_table_name: str,
        columns: list[Column],
    ) -> str | Executable:
        """Generate an insert statement for the given records.

        Args:
            full_table_name: the target table name.
            columns: a list of columns to put into the generated insert statement.

        Returns:
            An insert statement.
        """
        metadata = MetaData()
        table = Table(full_table_name, metadata, *columns)
        return insert(table)

    def conform_name(
        self,
        name: str,
        object_type: str | None = None,  # noqa: ARG002
    ) -> str:
        """Conforming names of tables, schemas, column names."""
        return name

    @property
    def schema_name(self) -> str | None:
        """Return the schema name or `None` if using names with no schema part.

                Note that after the next SDK release (after 0.14.0) we can remove this
                as it's already upstreamed.

        Returns:
            The target schema name.
        """
        # Look for a default_target_scheme in the configuraion fle
        default_target_schema: str = self.config.get("default_target_schema", None)
        parts = self.stream_name.split("-")

        # 1) When default_target_scheme is in the configuration use it
        # 2) if the streams are in <schema>-<table> format use the
        #    stream <schema>
        # 3) Return None if you don't find anything
        if default_target_schema:
            return default_target_schema

        if len(parts) in {2, 3}:
            # Stream name is a two-part or three-part identifier.
            # Use the second-to-last part as the schema name.
            return self.conform_name(parts[-2], "schema")

        # Schema name not detected.
        return None

    def activate_version(self, new_version: int) -> None:
        """Bump the active version of the target table.

        Args:
            new_version: The version number to activate.
        """
        # There's nothing to do if the table doesn't exist yet
        # (which it won't the first time the stream is processed)
        if not self.connector.table_exists(self.full_table_name):
            return

        deleted_at = now()
        # Different from SingerSDK as we need to handle types the
        # same as SCHEMA messsages
        datetime_type = self.connector.to_sql_type(
            {"type": "string", "format": "date-time"},
        )

        # Different from SingerSDK as we need to handle types the
        # same as SCHEMA messsages
        integer_type = self.connector.to_sql_type({"type": "integer"})

        if not self.connector.column_exists(
            full_table_name=self.full_table_name,
            column_name=self.version_column_name,
        ):
            self.connector.prepare_column(
                self.full_table_name,
                self.version_column_name,
                sql_type=integer_type,
            )

        self.logger.info("Hard delete: %s", self.config.get("hard_delete"))
        if self.config["hard_delete"] is True:
            self.connection.execute(
                # TODO: query injection?
                f"DELETE FROM {self.full_table_name} "  # noqa: S608
                f"WHERE {self.version_column_name} <= {new_version} "
                f"OR {self.version_column_name} IS NULL",
            )
            return

        if not self.connector.column_exists(
            full_table_name=self.full_table_name,
            column_name=self.soft_delete_column_name,
        ):
            self.connector.prepare_column(
                self.full_table_name,
                self.soft_delete_column_name,
                sql_type=datetime_type,
            )
        # Need to deal with the case where data doesn't exist for the version column
        query = sqlalchemy.text(
            f"UPDATE {self.full_table_name}\n"
            f"SET {self.soft_delete_column_name} = :deletedate \n"
            f"WHERE {self.version_column_name} < :version "
            f"OR {self.version_column_name} IS NULL \n"
            f"  AND {self.soft_delete_column_name} IS NULL\n",
        )
        query = query.bindparams(
            bindparam("deletedate", value=deleted_at, type_=datetime_type),
            bindparam("version", value=new_version, type_=integer_type),
        )
        self.connector.connection.execute(query)
