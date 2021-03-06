"""
sqldiff.py - Prints the (approximated) difference between models and database

TODO:
 - better support for relations
 - better support for constraints (mainly postgresql?)
 - support for table spaces with postgresql
 - when a table is not managed (meta.managed==False) then only do a one-way
   sqldiff ? show differences from db->table but not the other way around since
   it's not managed.
 
KNOWN ISSUES:
 - MySQL has by far the most problems with introspection. Please be
   carefull when using MySQL with sqldiff. 
   - Booleans are reported back as Integers, so there's know way to know if
     there was a real change.
   - Varchar sizes are reported back without unicode support so there size
     may change in comparison to the real length of the varchar.   
   - Some of the 'fixes' to counter these problems might create false 
     positives or false negatives.
"""

from django.core.management.base import BaseCommand
from django.core.management import sql as _sql
from django.core.management import CommandError
from django.core.management.color import no_style
from django.db import transaction, connection
from django.db.models.fields import IntegerField
from optparse import make_option

ORDERING_FIELD = IntegerField('_order', null=True)

def flatten(l, ltypes=(list, tuple)):
    ltype = type(l)
    l = list(l)
    i = 0
    while i < len(l):
        while isinstance(l[i], ltypes):
            if not l[i]:
                l.pop(i)
                i -= 1
                break
            else:
                l[i:i + 1] = l[i]
        i += 1
    return ltype(l)

class SQLDiff(object):
    DATA_TYPES_REVERSE_OVERRIDE = {
    }
    
    DIFF_TYPES = [
        'comment',
        'table-missing-in-db',
        'field-missing-in-db',
        'field-missing-in-model',
        'index-missing-in-db',
        'index-missing-in-model',
        'unique-missing-in-db',
        'unique-missing-in-model',
        'field-type-differ',
        'field-parameter-differ',
    ]
    DIFF_TEXTS = {
        'comment': 'comment: %(0)s',
        'table-missing-in-db': "table '%(0)s' missing in database",
        'field-missing-in-db' : "field '%(1)s' defined in model but missing in database",
        'field-missing-in-model' : "field '%(1)s' defined in database but missing in model",
        'index-missing-in-db' : "field '%(1)s' INDEX defined in model but missing in database",
        'index-missing-in-model' : "field '%(1)s' INDEX defined in database schema but missing in model",
        'unique-missing-in-db' : "field '%(1)s' UNIQUE defined in model but missing in database",
        'unique-missing-in-model' : "field '%(1)s' UNIQUE defined in database schema but missing in model",
        'field-type-differ' : "field '%(1)s' not of same type: db='%(3)s', model='%(2)s'",
        'field-parameter-differ' : "field '%(1)s' parameters differ: db='%(3)s', model='%(2)s'",
    }

    SQL_FIELD_MISSING_IN_DB = lambda self, style, qn, args: "%s %s\n\t%s %s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD('ADD'), style.SQL_FIELD(qn(args[1])), style.SQL_COLTYPE(args[2]))
    SQL_FIELD_MISSING_IN_MODEL = lambda self, style, qn, args: "%s %s\n\t%s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD('DROP COLUMN'), style.SQL_FIELD(qn(args[1])))
    SQL_INDEX_MISSING_IN_DB = lambda self, style, qn, args: "%s %s\n\t%s %s (%s);" % (style.SQL_KEYWORD('CREATE INDEX'), style.SQL_TABLE(qn("%s_idx" % '_'.join(args[0:2]))), style.SQL_KEYWORD('ON'), style.SQL_TABLE(qn(args[0])), style.SQL_FIELD(qn(args[1])))
    # FIXME: need to lookup index name instead of just appending _idx to table + fieldname
    SQL_INDEX_MISSING_IN_MODEL = lambda self, style, qn, args: "%s %s;" % (style.SQL_KEYWORD('DROP INDEX'), style.SQL_TABLE(qn("%s_idx" % '_'.join(args[0:2]))))
    SQL_UNIQUE_MISSING_IN_DB = lambda self, style, qn, args: "%s %s\n\t%s %s (%s);" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD('ADD'), style.SQL_KEYWORD('UNIQUE'), style.SQL_FIELD(qn(args[1])))
    # FIXME: need to lookup unique constraint name instead of appending _key to table + fieldname
    SQL_UNIQUE_MISSING_IN_MODEL = lambda self, style, qn, args: "%s %s\n\t%s %s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD('DROP'), style.SQL_KEYWORD('CONSTRAINT'), style.SQL_TABLE(qn("%s_key" % ('_'.join(args[:2])))))
    SQL_FIELD_TYPE_DIFFER = lambda self, style, qn, args:  "%s %s\n\t%s %s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD("MODIFY"), style.SQL_FIELD(qn(args[1])), style.SQL_COLTYPE(args[2]))
    SQL_FIELD_PARAMETER_DIFFER = lambda self, style, qn, args:  "%s %s\n\t%s %s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD("MODIFY"), style.SQL_FIELD(qn(args[1])), style.SQL_COLTYPE(args[2]))
    SQL_COMMENT = lambda self, style, qn, args: style.NOTICE('-- Comment: %s' % style.SQL_TABLE(args[0]))
    SQL_TABLE_MISSING_IN_DB = lambda self, style, qn, args: style.NOTICE('-- Table missing: %s' % args[0])

    def __init__(self, app_models, options):
        self.app_models = app_models
        self.options = options
        self.dense = options.get('dense_output', False)

        try:
            self.introspection = connection.introspection
        except AttributeError:
            from django.db import get_introspection_module
            self.introspection = get_introspection_module()

        self.cursor = connection.cursor()
        self.django_tables = self.get_django_tables(options.get('only_existing', True))
        self.db_tables = self.introspection.get_table_list(self.cursor)
        self.differences = []
        self.unknown_db_fields = {}
        
        self.DIFF_SQL = {
            'comment': self.SQL_COMMENT,
            'table-missing-in-db': self.SQL_TABLE_MISSING_IN_DB,
            'field-missing-in-db': self.SQL_FIELD_MISSING_IN_DB,
            'field-missing-in-model': self.SQL_FIELD_MISSING_IN_MODEL,
            'index-missing-in-db': self.SQL_INDEX_MISSING_IN_DB,
            'index-missing-in-model': self.SQL_INDEX_MISSING_IN_MODEL,
            'unique-missing-in-db': self.SQL_UNIQUE_MISSING_IN_DB,
            'unique-missing-in-model': self.SQL_UNIQUE_MISSING_IN_MODEL,
            'field-type-differ': self.SQL_FIELD_TYPE_DIFFER,
            'field-parameter-differ': self.SQL_FIELD_PARAMETER_DIFFER,
        }

    def add_app_model_marker(self, app_label, model_name):
        self.differences.append((app_label, model_name, []))
        
    def add_difference(self, diff_type, *args):
        assert diff_type in self.DIFF_TYPES, 'Unknown difference type'
        self.differences[-1][-1].append((diff_type, args))

    def get_django_tables(self, only_existing):
        try:
            django_tables = self.introspection.django_table_names(only_existing=only_existing)
        except AttributeError:
            # backwards compatibility for before introspection refactoring (r8296)
            try:
                django_tables = _sql.django_table_names(only_existing=only_existing)
            except AttributeError:
                # backwards compatibility for before svn r7568
                django_tables = _sql.django_table_list(only_existing=only_existing)
        return django_tables

    def sql_to_dict(self, query,param):
        """ sql_to_dict(query, param) -> list of dicts

        code from snippet at http://www.djangosnippets.org/snippets/1383/
        """
        cursor = connection.cursor()
        cursor.execute(query,param)
        fieldnames = [name[0] for name in cursor.description]
        result = []
        for row in cursor.fetchall():
            rowset = []
            for field in zip(fieldnames, row):
                rowset.append(field)
            result.append(dict(rowset))
        return result

    def get_field_model_type(self, field):
        return field.db_type()

    def get_field_db_type(self, description, field=None, table_name=None):
        from django.db import models
        # DB-API cursor.description
        #(name, type_code, display_size, internal_size, precision, scale, null_ok) = description
        type_code = description[1]
        if type_code in self.DATA_TYPES_REVERSE_OVERRIDE:
            reverse_type = self.DATA_TYPES_REVERSE_OVERRIDE[type_code]
        else:
            try:
                try:
                    reverse_type = self.introspection.data_types_reverse[type_code]
                except AttributeError:
                    # backwards compatibility for before introspection refactoring (r8296)
                    reverse_type = self.introspection.DATA_TYPES_REVERSE.get(type_code)
            except KeyError:
                # type_code not found in data_types_reverse map
                key = (self.differences[-1][:2], description[:2])
                if key not in self.unknown_db_fields:
                    self.unknown_db_fields[key] = 1
                    self.add_difference('comment', "Unknown database type for field '%s' (%s)" % (description[0], type_code))
                return None
        
        kwargs = {}
        if isinstance(reverse_type, tuple):
            kwargs.update(reverse_type[1])
            reverse_type = reverse_type[0]

        if reverse_type == "CharField" and description[3]:
            kwargs['max_length'] = description[3]

        if reverse_type == "DecimalField":
            kwargs['max_digits'] = description[4]
            kwargs['decimal_places'] = abs(description[5])

        if description[6]:
            kwargs['blank'] = True
            if not reverse_type in ('TextField', 'CharField'):
                kwargs['null'] = True
        
        if '.' in reverse_type:
            from django.utils import importlib
            # TODO: when was importlib added to django.utils ? and do we
            # need to add backwards compatibility code ?
            module_path, package_name = reverse_type.rsplit('.', 1)
            module = importlib.import_module(module_path)
            field_db_type = getattr(module, package_name)(**kwargs).db_type()
        else:
            field_db_type = getattr(models, reverse_type)(**kwargs).db_type()
        return field_db_type

    def strip_parameters(self, field_type):
        if field_type:
            return field_type.split(" ")[0].split("(")[0]
        return field_type

    def find_unique_missing_in_db(self, meta, table_indexes, table_name):
        for field in meta.local_fields:
            if field.unique:
                attname = field.db_column or field.attname
                if attname in table_indexes and table_indexes[attname]['unique']:
                    continue
                self.add_difference('unique-missing-in-db', table_name, attname)

    def find_unique_missing_in_model(self, meta, table_indexes, table_name):
        # TODO: Postgresql does not list unique_togethers in table_indexes
        #       MySQL does
        fields = dict([(field.db_column or field.name, field.unique) for field in meta.local_fields])
        for att_name, att_opts in table_indexes.iteritems():
            if att_opts['unique'] and att_name in fields and not fields[att_name]:
                if att_name in flatten(meta.unique_together): continue
                self.add_difference('unique-missing-in-model', table_name, att_name)

    def find_index_missing_in_db(self, meta, table_indexes, table_name):
        for field in meta.local_fields:
            if field.db_index:
                attname = field.db_column or field.attname
                if not attname in table_indexes:
                    self.add_difference('index-missing-in-db', table_name, attname)

    def find_index_missing_in_model(self, meta, table_indexes, table_name):
        fields = dict([(field.name, field) for field in meta.local_fields])
        for att_name, att_opts in table_indexes.iteritems():
            if att_name in fields:
                field = fields[att_name]
                if field.db_index: continue
                if att_opts['primary_key'] and field.primary_key: continue
                if att_opts['unique'] and field.unique: continue
                if att_opts['unique'] and att_name in flatten(meta.unique_together): continue
                self.add_difference('index-missing-in-model', table_name, att_name)

    def find_field_missing_in_model(self, fieldmap, table_description, table_name):
        for row in table_description:
            if row[0] not in fieldmap:
                self.add_difference('field-missing-in-model', table_name, row[0])

    def find_field_missing_in_db(self, fieldmap, table_description, table_name):
        db_fields = [row[0] for row in table_description]
        for field_name, field in fieldmap.iteritems():
            if field_name not in db_fields:
                self.add_difference('field-missing-in-db', table_name, field_name, field.db_type())

    def find_field_type_differ(self, meta, table_description, table_name, func=None):
        db_fields = dict([(row[0], row) for row in table_description])
        for field in meta.local_fields:
            if field.name not in db_fields: continue
            description = db_fields[field.name]

            model_type = self.strip_parameters(self.get_field_model_type(field))
            db_type = self.strip_parameters(self.get_field_db_type(description, field))

            # use callback function if defined
            if func:
                model_type, db_type = func(field, description, model_type, db_type)

            if not model_type==db_type:
                self.add_difference('field-type-differ', table_name, field.name, model_type, db_type)

    def find_field_parameter_differ(self, meta, table_description, table_name, func=None):
        db_fields = dict([(row[0], row) for row in table_description])
        for field in meta.local_fields:
            if field.name not in db_fields: continue
            description = db_fields[field.name]

            model_type = self.get_field_model_type(field)
            db_type = self.get_field_db_type(description, field, table_name)

            if not self.strip_parameters(model_type)==self.strip_parameters(db_type):
                continue
            
            # use callback function if defined
            if func:
                model_type, db_type = func(field, description, model_type, db_type)

            if not model_type==db_type:
                self.add_difference('field-parameter-differ', table_name, field.name, model_type, db_type)
    
    @transaction.commit_manually
    def find_differences(self):
        cur_app_label = None
        for app_model in self.app_models:
            meta = app_model._meta
            table_name = meta.db_table
            app_label = meta.app_label

            if cur_app_label!=app_label:
                # Marker indicating start of difference scan for this table_name
                self.add_app_model_marker(app_label, app_model.__name__)

            #if not table_name in self.django_tables:
            if not table_name in self.db_tables:
                # Table is missing from database
                self.add_difference('table-missing-in-db', table_name)
                continue
            
            table_indexes = self.introspection.get_indexes(self.cursor, table_name)
            fieldmap = dict([(field.db_column or field.get_attname(), field) for field in meta.local_fields])
            
            # add ordering field if model uses order_with_respect_to
            if meta.order_with_respect_to:
                fieldmap['_order'] = ORDERING_FIELD

            try:
                table_description = self.introspection.get_table_description(self.cursor, table_name)
            except Exception, e:
                model_diffs.append((app_model.__name__, [str(e).strip()]))
                transaction.rollback() # reset transaction
                continue
            
            # Fields which are defined in database but not in model
            # 1) find: 'unique-missing-in-model'
            self.find_unique_missing_in_model(meta, table_indexes, table_name)
            # 2) find: 'index-missing-in-model'
            self.find_index_missing_in_model(meta, table_indexes, table_name)
            # 3) find: 'field-missing-in-model'
            self.find_field_missing_in_model(fieldmap, table_description, table_name)

            # Fields which are defined in models but not in database
            # 4) find: 'field-missing-in-db'
            self.find_field_missing_in_db(fieldmap, table_description, table_name)
            # 5) find: 'unique-missing-in-db'
            self.find_unique_missing_in_db(meta, table_indexes, table_name)
            # 6) find: 'index-missing-in-db'
            self.find_index_missing_in_db(meta, table_indexes, table_name)

            # Fields which have a different type or parameters
            # 7) find: 'type-differs'
            self.find_field_type_differ(meta, table_description, table_name)
            # 8) find: 'type-parameter-differs'
            self.find_field_parameter_differ(meta, table_description, table_name)

    def print_diff(self, style=no_style()):
        """ print differences to stdout """
        if self.options.get('sql', True):
            self.print_diff_sql(style)
        else:
            self.print_diff_text(style)

    def print_diff_text(self, style):
        cur_app_label = None
        for app_label, model_name, diffs in self.differences:
            if not diffs: continue
            if not self.dense and cur_app_label != app_label:
                print style.NOTICE("+ Application:"), style.SQL_TABLE(app_label)
                cur_app_label = app_label
            if not self.dense:
                print style.NOTICE("|-+ Differences for model:"), style.SQL_TABLE(model_name)
            for diff in diffs:
                diff_type, diff_args = diff
                text = self.DIFF_TEXTS[diff_type] % dict((str(i), style.SQL_TABLE(e)) for i, e in enumerate(diff_args))
                text = "'".join(i%2==0 and style.ERROR(e) or e for i, e in enumerate(text.split("'")))
                if not self.dense:
                    print style.NOTICE("|--+"), text
                else:
                    print style.NOTICE("App"), style.SQL_TABLE(app_name), style.NOTICE('Model'), style.SQL_TABLE(model_name), text

    def print_diff_sql(self, style):
        cur_app_label = None
        qn = connection.ops.quote_name
        print style.SQL_KEYWORD("BEGIN;")
        for app_label, model_name, diffs in self.differences:
            if not diffs: continue
            if not self.dense and cur_app_label != app_label:
                print style.NOTICE("-- Application: %s" % style.SQL_TABLE(app_label))
                cur_app_label = app_label
            if not self.dense:
                print style.NOTICE("-- Model: %s" % style.SQL_TABLE(model_name))
            for diff in diffs:
                diff_type, diff_args = diff
                text = self.DIFF_SQL[diff_type](style, qn, diff_args)
                if self.dense:
                    text = text.replace("\n\t", " ")
                print text
        print style.SQL_KEYWORD("COMMIT;")
        
class GenericSQLDiff(SQLDiff):
    pass

class MySQLDiff(SQLDiff):
    # All the MySQL hacks together create something of a problem
    # Fixing one bug in MySQL creates another issue. So just keep in mind
    # that this is way unreliable for MySQL atm.
    def get_field_db_type(self, description, field=None, table_name=None):
        from MySQLdb.constants import FIELD_TYPE
        # weird bug? in mysql db-api where it returns three times the correct value for field length
        # if i remember correctly it had something todo with unicode strings
        # TODO: Fix this is a more meaningful and better understood manner
        description = list(description)
        if description[1] not in [FIELD_TYPE.TINY, FIELD_TYPE.SHORT]: # exclude tinyints from conversion.
            description[3] = description[3]/3
            description[4] = description[4]/3
        db_type = super(MySQLDiff, self).get_field_db_type(description)
        if not db_type:
            return
        if field:
            if field.primary_key and (db_type=='integer' or db_type=='bigint'):
                db_type += ' AUTO_INCREMENT'
            # MySQL isn't really sure about char's and varchar's like sqlite
            field_type = self.get_field_model_type(field)
            # Fix char/varchar inconsistencies
            if self.strip_parameters(field_type)=='char' and self.strip_parameters(db_type)=='varchar':
                db_type = db_type.lstrip("var")
            # They like to call 'bool's 'tinyint(1)' and introspection makes that a integer
            # just convert it back to it's proper type, a bool is a bool and nothing else.
            if db_type=='integer' and description[1]==FIELD_TYPE.TINY and description[4]==1:
                db_type = 'bool'
            if db_type=='integer' and description[1]==FIELD_TYPE.SHORT:
                db_type = 'smallint UNSIGNED' # FIXME: what about if it's not UNSIGNED ?
        return db_type

class SqliteSQLDiff(SQLDiff):
    # Unique does not seem to be implied on Sqlite for Primary_key's
    # if this is more generic among databases this might be usefull
    # to add to the superclass's find_unique_missing_in_db method
    def find_unique_missing_in_db(self, meta, table_indexes, table_name):
        for field in meta.local_fields:
            if field.unique:
                attname = field.db_column or field.attname
                if attname in table_indexes and table_indexes[attname]['unique']:
                    continue
                if table_indexes[attname]['primary_key']:
                    continue
                self.add_difference('unique-missing-in-db', table_name, attname)

    # Finding Indexes by using the get_indexes dictionary doesn't seem to work
    # for sqlite.
    def find_index_missing_in_db(self, meta, table_indexes, table_name):
        pass

    def find_index_missing_in_model(self, meta, table_indexes, table_name):
        pass

    def get_field_db_type(self, description, field=None, table_name=None):
        db_type = super(SqliteSQLDiff, self).get_field_db_type(description)
        if not db_type:
            return
        if field:
            field_type = self.get_field_model_type(field)
            # Fix char/varchar inconsistencies
            if self.strip_parameters(field_type)=='char' and self.strip_parameters(db_type)=='varchar':
                db_type = db_type.lstrip("var")
        return db_type

class PostgresqlSQLDiff(SQLDiff):
    DATA_TYPES_REVERSE_OVERRIDE = {
        20: 'IntegerField',
        1042: 'CharField',
        # postgis types (TODO: support is very incomplete)
        17506: 'django.contrib.gis.db.models.fields.PointField',
        55902: 'django.contrib.gis.db.models.fields.MultiPolygonField',
    }

    # Hopefully in the future we can add constraint checking and other more
    # advanced checks based on this database.
    SQL_LOAD_CONSTRAINTS = """
    SELECT nspname, relname, conname, attname, pg_get_constraintdef(pg_constraint.oid)
    FROM pg_constraint
    INNER JOIN pg_attribute ON pg_constraint.conrelid = pg_attribute.attrelid AND pg_attribute.attnum = any(pg_constraint.conkey)
    INNER JOIN pg_class ON conrelid=pg_class.oid
    INNER JOIN pg_namespace ON pg_namespace.oid=pg_class.relnamespace
    ORDER BY CASE WHEN contype='f' THEN 0 ELSE 1 END,contype,nspname,relname,conname;
    """

    SQL_FIELD_TYPE_DIFFER = lambda self, style, qn, args:  "%s %s\n\t%s %s %s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD('ALTER'), style.SQL_FIELD(qn(args[1])), style.SQL_KEYWORD("TYPE"), style.SQL_COLTYPE(args[2]))
    SQL_FIELD_PARAMETER_DIFFER = lambda self, style, qn, args:  "%s %s\n\t%s %s %s %s;" % (style.SQL_KEYWORD('ALTER TABLE'), style.SQL_TABLE(qn(args[0])), style.SQL_KEYWORD('ALTER'), style.SQL_FIELD(qn(args[1])), style.SQL_KEYWORD("TYPE"), style.SQL_COLTYPE(args[2]))

    def __init__(self, app_models, options):
        SQLDiff.__init__(self, app_models, options)
        self.check_constraints = {}
        self.load_constraints()

    def load_constraints(self):
        for dct in self.sql_to_dict(self.SQL_LOAD_CONSTRAINTS, []):
            key = (dct['nspname'], dct['relname'], dct['attname'])
            if 'CHECK' in dct['pg_get_constraintdef']:
                self.check_constraints[key] = dct

    def get_field_db_type(self, description, field=None, table_name=None):
        db_type = super(PostgresqlSQLDiff, self).get_field_db_type(description)
        if not db_type:
            return
        if field:
            if field.primary_key and db_type=='integer':
                db_type = 'serial'
            if table_name:
                tablespace = field.db_tablespace
                if tablespace=="":
                    tablespace = "public"
                check_constraint = self.check_constraints.get((tablespace, table_name, field.attname),{}).get('pg_get_constraintdef', None)
                if check_constraint:
                    check_constraint = check_constraint.replace("((", "(")
                    check_constraint = check_constraint.replace("))", ")")
                    check_constraint = '("'.join([')' in e and '" '.join(e.split(" ", 1)) or e for e in check_constraint.split("(")])
                    # TODO: might be more then one constraint in definition ?
                    db_type += ' '+check_constraint
        return db_type

    """
    def find_field_type_differ(self, meta, table_description, table_name):
        def callback(field, description, model_type, db_type):
            if field.primary_key and db_type=='integer':
                db_type = 'serial'
            return model_type, db_type
        super(PostgresqlSQLDiff, self).find_field_type_differs(meta, table_description, table_name, callback)
    """

DATABASE_SQLDIFF_CLASSES = {
    'postgresql_psycopg2' : PostgresqlSQLDiff,
    'postgresql': PostgresqlSQLDiff,
    'mysql': MySQLDiff,
    'sqlite3': SqliteSQLDiff,
    'oracle': GenericSQLDiff
}

class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option('--all-applications', '-a', action='store_true', dest='all_applications',
                    help="Automaticly include all application from INSTALLED_APPS."),
        make_option('--not-only-existing', '-e', action='store_false', dest='only_existing',
                    help="Check all tables that exist in the database, not only tables that should exist based on models."),
        make_option('--dense-output', '-d', action='store_true', dest='dense_output',
                    help="Shows the output in dense format, normally output is spreaded over multiple lines."),
        make_option('--output_text', '-t', action='store_false', dest='sql', default=True,
                    help="Outputs the differences as descriptive text instead of SQL"),
    )
    
    help = """Prints the (approximated) difference between models and fields in the database for the given app name(s).

It indicates how columns in the database are different from the sql that would
be generated by Django. This command is not a database migration tool. (Though
it can certainly help) It's purpose is to show the current differences as a way
to check/debug ur models compared to the real database tables and columns."""

    output_transaction = False
    args = '<appname appname ...>'

    def handle(self, *app_labels, **options):
        from django import VERSION
        if VERSION[:2]<(1,0):
            raise CommandError("SQLDiff only support Django 1.0 or higher!")

        from django.db import models
        from django.conf import settings

        if settings.DATABASE_ENGINE =='dummy':
            # This must be the "dummy" database backend, which means the user
            # hasn't set DATABASE_ENGINE.
            raise CommandError("Django doesn't know which syntax to use for your SQL statements,\n" +
                "because you haven't specified the DATABASE_ENGINE setting.\n" +
                "Edit your settings file and change DATABASE_ENGINE to something like 'postgresql' or 'mysql'.")

        if options.get('all_applications', False):
            app_models = models.get_models()
        else:
            if not app_labels:
                raise CommandError('Enter at least one appname.')
            try:
                app_list = [models.get_app(app_label) for app_label in app_labels]
            except (models.ImproperlyConfigured, ImportError), e:
                raise CommandError("%s. Are you sure your INSTALLED_APPS setting is correct?" % e)

            app_models = []
            for app in app_list:
                app_models.extend(models.get_models(app))

        ## remove all models that are not managed by Django
        #app_models = [model for model in app_models if getattr(model._meta, 'managed', True)]

        if not app_models:
            raise CommandError('Unable to execute sqldiff no models founds.')

        cls = DATABASE_SQLDIFF_CLASSES.get(settings.DATABASE_ENGINE, GenericSQLDiff)
        sqldiff_instance = cls(app_models, options)
        sqldiff_instance.find_differences()
        sqldiff_instance.print_diff(self.style)
        return
