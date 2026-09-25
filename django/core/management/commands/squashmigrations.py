import itertools
import os
import shutil

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import run_formatters
from django.db import migrations, models
from django.db.migrations.loader import AmbiguityError, MigrationLoader
from django.db.migrations.migration import SwappableTuple
from django.db.migrations.operations.base import OperationCategory
from django.db.migrations.operations.fields import FieldOperation
from django.db.migrations.operations.models import ModelOperation
from django.db.migrations.optimizer import MigrationOptimizer
from django.db.migrations.writer import MigrationWriter


class Command(BaseCommand):
    help = (
        "Squashes an existing set of migrations (from first until specified) into a "
        "single new one."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "app_label",
            help="App label of the application to squash migrations for.",
        )
        parser.add_argument(
            "start_migration_name",
            nargs="?",
            help=(
                "Migrations will be squashed starting from and including this "
                "migration."
            ),
        )
        parser.add_argument(
            "migration_name",
            help="Migrations will be squashed until and including this migration.",
        )
        parser.add_argument(
            "--ignore-dependencies",
            "--ignore-deps",
            action="store_true",
            dest="ignore_dependencies",
            help="Ignore external dependencies, except for those included in the"
            " initial migration.",
        )
        parser.add_argument(
            "--no-optimize",
            action="store_true",
            help="Do not try to optimize the squashed operations.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Tells Django to NOT prompt the user for input of any kind.",
        )
        parser.add_argument(
            "--squashed-name",
            help="Sets the name of the new squashed migration.",
        )
        parser.add_argument(
            "--no-header",
            action="store_false",
            dest="include_header",
            help="Do not add a header comment to the new squashed migration.",
        )

    def handle(self, **options):
        self.verbosity = options["verbosity"]
        self.interactive = options["interactive"]
        app_label = options["app_label"]
        start_migration_name = options["start_migration_name"]
        migration_name = options["migration_name"]
        ignore_dependencies = options["ignore_dependencies"]
        no_optimize = options["no_optimize"]
        squashed_name = options["squashed_name"]
        include_header = options["include_header"]
        # Validate app_label.
        try:
            apps.get_app_config(app_label)
        except LookupError as err:
            raise CommandError(str(err))
        # Load the current graph state, check the app and migration they asked
        # for exists.
        loader = MigrationLoader(None)
        if app_label not in loader.migrated_apps:
            raise CommandError(
                "App '%s' does not have migrations (so squashmigrations on "
                "it makes no sense)" % app_label
            )

        migration = self.find_migration(loader, app_label, migration_name)

        # Work out the list of predecessor migrations
        migrations_to_squash = [
            loader.get_migration(al, mn)
            for al, mn in loader.graph.forwards_plan(
                (migration.app_label, migration.name)
            )
            if al == migration.app_label
        ]

        if start_migration_name:
            start_migration = self.find_migration(
                loader, app_label, start_migration_name
            )
            start = loader.get_migration(
                start_migration.app_label, start_migration.name
            )
            try:
                start_index = migrations_to_squash.index(start)
                migrations_to_squash = migrations_to_squash[start_index:]
            except ValueError:
                raise CommandError(
                    "The migration '%s' cannot be found. Maybe it comes after "
                    "the migration '%s'?\n"
                    "Have a look at:\n"
                    "  python manage.py showmigrations %s\n"
                    "to debug this issue." % (start_migration, migration, app_label)
                )

        # Tell them what we're doing and optionally ask if we should proceed
        if self.verbosity > 0 or self.interactive:
            self.stdout.write(
                self.style.MIGRATE_HEADING("Will squash the following migrations:")
            )
            for migration in migrations_to_squash:
                self.stdout.write(" - %s" % migration.name)

            if ignore_dependencies:
                self.stdout.write(
                    self.style.NOTICE(
                        "To avoid cross-app dependencies, operations "
                        "creating such dependencies will be ignored."
                    )
                )

            if self.interactive:
                answer = None
                while not answer or answer not in "yn":
                    answer = input("Do you wish to proceed? [y/N] ")
                    if not answer:
                        answer = "n"
                        break
                    else:
                        answer = answer[0].lower()
                if answer != "y":
                    return

        # Load the operations from all those migrations and concat together,
        # along with collecting external dependencies and detecting
        # double-squashing.
        # First collect all operations
        operations = list(
            itertools.chain.from_iterable(m.operations for m in migrations_to_squash)
        )
        # Dependencies for the first migration are always incliuded
        dependencies = set(migrations_to_squash[0].dependencies)

        # Collect dependencies
        for smigration in migrations_to_squash[1:]:
            for dependency in smigration.dependencies:
                if isinstance(dependency, SwappableTuple):
                    # This is a dependency we probably want to preserve
                    dependencies.add(dependency)
                else:
                    mig_app_label, mig_name = dependency
                    if mig_app_label == app_label:
                        # Internal dependency -- we want to add unless it is being squashed
                        if mig_name not in (m.name for m in migrations_to_squash):
                            dependencies.add(dependency)
                    elif not ignore_dependencies:
                        dependencies.add(dependency)

        if no_optimize:
            if self.verbosity > 0:
                self.stdout.write(
                    self.style.MIGRATE_HEADING("(Skipping optimization.)")
                )
            new_operations = operations
        else:
            if self.verbosity > 0:
                self.stdout.write(self.style.MIGRATE_HEADING("Optimizing..."))

            optimizer = MigrationOptimizer()
            new_operations = optimizer.optimize(operations, migration.app_label)

            if self.verbosity > 0:
                if len(new_operations) == len(operations):
                    self.stdout.write("  No optimizations possible.")
                else:
                    self.stdout.write(
                        "  Optimized from %s operations to %s operations."
                        % (len(operations), len(new_operations))
                    )

        if ignore_dependencies:
            # filter operations
            # In principle, no need to filter ops from first migration
            # but after optimization, some of them may already be gone...
            # so need to identify not by their number, but by presence
            # in the list of operations
            # (assuming optimizer doesn't change ids)
            # ---> Optimizer does change ids.
            # ---> We can identify them by the dependencies. Any operation
            #      with external dependencies included in those of the first
            #      migration, can stay
            self.filter_cross_app_operations(app_label, new_operations)
        replaces = [(m.app_label, m.name) for m in migrations_to_squash]

        # Make a new migration with those operations
        subclass = type(
            "Migration",
            (migrations.Migration,),
            {
                "dependencies": dependencies,
                "operations": new_operations,
                "replaces": replaces,
            },
        )
        if start_migration_name:
            if squashed_name:
                # Use the name from --squashed-name.
                prefix, _ = start_migration.name.split("_", 1)
                name = "%s_%s" % (prefix, squashed_name)
            else:
                # Generate a name.
                name = "%s_squashed_%s" % (start_migration.name, migration.name)
            new_migration = subclass(name, app_label)
        else:
            name = "0001_%s" % (squashed_name or "squashed_%s" % migration.name)
            new_migration = subclass(name, app_label)
            new_migration.initial = True

        # Write out the new migration file
        writer = MigrationWriter(new_migration, include_header)
        if os.path.exists(writer.path):
            raise CommandError(
                f"Migration {new_migration.name} already exists. Use a different name."
            )
        with open(writer.path, "w", encoding="utf-8") as fh:
            fh.write(writer.as_string())
        run_formatters([writer.path], stderr=self.stderr)

        if self.verbosity > 0:
            self.stdout.write(
                self.style.MIGRATE_HEADING(
                    "Created new squashed migration %s" % writer.path
                )
                + "\n"
                "  You should commit this migration but leave the old ones in place;\n"
                "  the new migration will be used for new installs. Once you are sure\n"
                "  all instances of the codebase have applied the migrations you "
                "squashed,\n"
                "  you can delete them."
            )
            if writer.needs_manual_porting:
                self.stdout.write(
                    self.style.MIGRATE_HEADING("Manual porting required") + "\n"
                    "  Your migrations contained functions that must be manually "
                    "copied over,\n"
                    "  as we could not safely copy their implementation.\n"
                    "  See the comment at the top of the squashed migration for "
                    "details."
                )
                if shutil.which("black"):
                    self.stdout.write(
                        self.style.WARNING(
                            "Squashed migration couldn't be formatted using the "
                            '"black" command. You can call it manually.'
                        )
                    )

    def _is_multiapp_operation(self, app_label, operation):
        return self._is_multiapp_field_operation(
            app_label, operation
        ) or self._is_multiapp_model_operation(app_label, operation)

    def _is_multiapp_field_operation(self, app_label, operation):
        return (
            isinstance(operation, FieldOperation)
            and isinstance(operation.field, models.fields.related.RelatedField)
            and operation.field.related_model.split(".")[0] != app_label
        )

    def _is_multiapp_model_operation(self, app_label, operation):
        if isinstance(operation, ModelOperation):
            for field in operation.fields:
                if (
                    isinstance(field[-1], models.fields.related.RelatedField)
                    and not field[-1].related_model.split(".")[0] != app_label
                ):
                    return True

    def filter_cross_app_operations(self, app_label, operations):
        filtered_operations = []
        ignore_restricted_categories = set(
            [OperationCategory.ADDITION, OperationCategory.ALTERATION]
        )

        for operation in operations:
            if (operation.category not in ignore_restricted_categories) or (
                operation.category in ignore_restricted_categories
                and not self._is_multiapp_operation(app_label, operation)
            ):
                filtered_operations.append(operation)

        return filtered_operations

    def find_migration(self, loader, app_label, name):
        try:
            return loader.get_migration_by_prefix(app_label, name)
        except AmbiguityError:
            raise CommandError(
                "More than one migration matches '%s' in app '%s'. Please be "
                "more specific." % (name, app_label)
            )
        except KeyError:
            raise CommandError(
                "Cannot find a migration matching '%s' from app '%s'."
                % (name, app_label)
            )
