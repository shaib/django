from __future__ import unicode_literals

import unittest
from copy import copy

from django import test
from django.db import connections, models, DEFAULT_DB_ALIAS
from django.db.models import Field

from .models import UpdateModel, SelectModel, RefreshModel


class UpdateOnlyTests(test.TestCase):
    """Tests for update-only fields."""

    def test_field_is_ignored_on_insert(self):
        UpdateModel.objects.create(f='text')
        self.assertFalse(UpdateModel.objects.all()[0].f)

    def test_field_is_set_on_update(self):
        m = UpdateModel.objects.create(f='text')
        m.save()
        self.assertEqual(UpdateModel.objects.all()[0].f, 'text')

    def test_field_is_fetched_on_select(self):
        UpdateModel.objects.create(f='text')
        m = UpdateModel.objects.all()[0]
        m.f  # Access f to force its evaluation.
        self.assertNumQueries(2)

    def test_db_default_fields_are_always_updated_on_raw_queries(self):
        m = SelectModel.objects.create()
        m.f = 'text'
        m.save_base(raw=True)
        m = SelectModel.objects.all()[0]
        self.assertEqual(m.f, 'text')


class SelectOnlyTests(test.TestCase):
    """Tests for select-only fields."""

    def test_field_is_ignored_on_insert(self):
        SelectModel.objects.create(f='text')
        self.assertFalse(SelectModel.objects.all()[0].f)

    def test_field_is_ignored_on_update(self):
        m = SelectModel.objects.create(f='text')
        m.f = 'text'
        m.save()
        self.assertFalse(SelectModel.objects.all()[0].f)

    def test_field_is_fetched_on_select(self):
        SelectModel.objects.create(f='text')
        m = SelectModel.objects.all()[0]
        m.f  # Access f to force its evaluation.
        self.assertNumQueries(2)

    def test_update_method_does_update_the_field(self):
        SelectModel.objects.create(f='text')
        SelectModel.objects.update(f='text2')
        m = SelectModel.objects.all()[0]
        self.assertEqual(m.f, 'text2')

    def test_db_default_fields_are_always_inserted_on_raw_queries(self):
        # Fixture loading depends on raw == True
        m = SelectModel(f='text')
        m.save_base(raw=True)
        m = SelectModel.objects.all()[0]
        self.assertEqual(m.f, 'text')


class PrimaryKeysUseOnInsertUpdateTestCase(unittest.TestCase):

    def test_pk_and_not_use_on_insert_raises_exception(self):
        self.assertRaises(
            ValueError, models.Field,
            primary_key=True, delegate_to_db=Field.CREATE
        )

    def test_pk_and_not_use_on_update_raises_exception(self):
        self.assertRaises(
            ValueError, models.Field,
            primary_key=True, delegate_to_db=Field.UPDATE
        )

    def test_autofield_always_sets_use_on_insert(self):
        f = models.AutoField(primary_key=True, delegate_to_db=Field.CREATE)
        self.assertTrue(f.use_on_insert)

    def test_autofield_always_sets_use_on_update(self):
        f = models.AutoField(primary_key=True, delegate_to_db=Field.UPDATE)
        self.assertTrue(f.use_on_update)


@unittest.skipIf(connections[DEFAULT_DB_ALIAS].vendor == 'sqlite',
                 "SET DEFAULT is not supported in sqlite3.")
class RefreshDbDefaultFieldsOnInsert(test.TestCase):

    @classmethod
    def setUpClass(cls):
        with connections[DEFAULT_DB_ALIAS].schema_editor() as schema_editor:
            f = SelectModel._meta.get_field_by_name('f')[0]
            copy_f = copy(f)
            f.default = u'default'

            schema_editor.alter_field(SelectModel, copy_f, f)
            f.default = models.fields.NOT_PROVIDED

    @classmethod
    def tearDownClass(cls):
        with connections[DEFAULT_DB_ALIAS].schema_editor() as schema_editor:
            f = SelectModel._meta.get_field_by_name('f')[0]
            copy_f = copy(f)
            f.default = u'default'
            schema_editor.alter_field(SelectModel, f, copy_f)
            f.default = models.fields.NOT_PROVIDED

    def test_force_fetch_is_false_does_not_refresh_fields(self):
        m = SelectModel()
        m.save(force_fetch=False)
        self.assertFalse(m.f)

    def test_force_fetch_is_true_refreshes_fields(self):
        m = SelectModel()
        m.save(force_fetch=True)
        self.assertEqual(m.f, 'default')


class RefreshDbDefaultFieldsOnUpdate(test.TestCase):

    def test_force_fetch_is_false_does_not_refresh_fields(self):
        m = SelectModel()
        m.save()
        SelectModel.objects.update(f='text')
        m.save()
        self.assertFalse(m.f)

    def test_force_fetch_is_true_refreshes_fields(self):
        m = RefreshModel()
        m.save()
        RefreshModel.objects.update(f='text')
        m.normal = 'Hi'
        m.save(force_fetch=True)
        self.assertEqual(m.f, 'text')

    def test_no_values_for_update_does_not_hit_the_database(self):
        m = SelectModel()
        m.save()
        SelectModel.objects.update(f='text')
        m.save(force_fetch=True)
        self.assertFalse(m.f)
