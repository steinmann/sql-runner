import csv
import io
import os
import re
import datetime
import traceback
import psycopg2

from utils import config, get_connection


class QueryList(list):
    actions = {
        'e': 'query',
        't': 'create_table_stmt',
        'v': 'create_view_stmt',
        'm': 'materialize_view_stmt'
    }

    def __init__(self, csv_string):
        self.cursor = get_connection().cursor()
        for query in csv.DictReader(io.StringIO(csv_string.strip()), delimiter=';'):
            self.append(Query(**query))

    @staticmethod
    def from_csv_files(path, csv_files):
        if not isinstance(csv_files, list):
            csv_files = [csv_files]
        print('read query lists from: {}'.format(', '.join(f for f in csv_files)))
        csv_string = ['schema_name;table_name;action']
        for file in csv_files:
            file_path = '{}/{}.csv'.format(path, file)
            with open(file_path, 'r') as f:
                csv_string.append(f.read().strip())
        return QueryList('\n'.join(csv_string))

    def test(self, schema_prefix='zz_'):
        schema_names = set(query.schema_name for query in self)
        for schema_name in schema_names:
            statements = """DROP SCHEMA IF EXISTS {schema_prefix}{schema_name} CASCADE;
                         CREATE SCHEMA {schema_prefix}{schema_name};
                         """.format(**locals())
            for stmt in statements.split(';'):
                self.cursor.execute(stmt)
        for query in self:
            query.schema_prefix = schema_prefix
            for schema_name in schema_names:
                query.action = 'v'
                query.query = query.query.replace(schema_name + '.', schema_prefix + schema_name + '.')
        self.execute()

    def execute(self):
        for query in self:
            print(query)
            if query.action in QueryList.actions:
                stmt_type = QueryList.actions[query.action]
                start = datetime.datetime.now()
                for stmt in getattr(query, stmt_type).split(';'):
                    if stmt.strip():
                        try:
                            self.cursor.execute(stmt)
                        except (psycopg2.ProgrammingError, psycopg2.InternalError):
                            msg = """
                            ERROR: executing '{query.name}':
                            SQL path "{query.path}"
                            {stmt}\n{msg}
                            """.replace(4 * 7 * ' ', '').format(msg=traceback.format_exc(), stmt=stmt, query=query)
                            print(msg)
                            exit(1)
                print(datetime.datetime.now()-start)


class Query(object):

    def __init__(self, schema_name, table_name, action):
        self.schema_name = schema_name.strip()
        self.schema_prefix = ''
        self.table_name = table_name.strip()
        self.action = action.strip()
        self.sql_path = config.sql_path

        path = '{self.sql_path}/{self.schema_name}/{self.table_name}.sql'.format(self=self)
        self.path = os.path.abspath(os.path.normpath(path))
        print(self.path)
        if not os.path.isfile(self.path):
            raise ValueError('file {} does not exist'.format(self.path))
        with open(self.path, 'r') as f:
            self.query = ''
            raw_query = f.readlines()
            for line in raw_query:
                # strip comments
                comment = re.search('--', line)
                if comment:
                    line = line[:comment.start()]
                self.query = self.query + line

    def __repr__(self):
        return '{self.schema_prefix}{self.schema_name}.{self.table_name} > {self.action}'.format(self=self)

    def check_uniqueness(self, cursor):
        if len(self.unique_keys) > 0:
            print('check uniqueness for {self.name}'.format(self=self))
            for key in self.unique_keys:
                select_key_stmt = """
                SELECT '{key}', COUNT(*)
                FROM (SELECT {key}
                      FROM {self.name}
                      WHERE {key} IS NOT NULL
                      GROUP BY 1
                      HAVING COUNT(*) > 1);
                """.format(self=self, key=key)
                cursor.execute(select_key_stmt)
                for line in cursor:
                    print(line)

    @property
    def name(self):
        return '{self.schema_prefix}{self.schema_name}.{self.table_name}'.format(self=self)

    @property
    def table_dependencies(self):
        return [str(match) for match in re.findall(r'(?:FROM|JOIN)\s*([a-zA-Z0-9_]*\.[a-zA-Z0-9_]*)(?:\s|;)',
                                                   self.query, re.DOTALL)]

    @property
    def select_stmt(self):
        match = re.search(r'((SELECT|WITH)(\'.*\'|[^;])*)(;|$)', self.query, re.DOTALL)
        if match is not None:
            select_stmt = match.group(1).strip()
            if select_stmt[-1] == ')' and select_stmt.count(')') > select_stmt.count('('):
                select_stmt = select_stmt[:-1]

        else:
            select_stmt = None
        return select_stmt

    @property
    def distkey_stmt(self):
        if config.database_type == 'redshift':
            match = re.search(r'/\*.*(distkey\s*\([^\()]*\)).*\**/', self.query, re.DOTALL | re.IGNORECASE)
            if match is not None:
                if match.group(1) == 'DISTKEY ()':
                    distkey_stmt = 'DISTSTYLE ALL'
                else:
                    distkey_stmt = 'DISTSTYLE KEY ' + match.group(1)
            else:
                distkey_stmt = 'DISTSTYLE EVEN'
            return distkey_stmt
        else:
            return ''

    @property
    def sortkey_stmt(self):
        if config.database_type == 'redshift':
            match = re.search(r'/\*.*((compound\s*sortkey|interleaved\s*sortkey)\s*\([^\()]*\)).*\**/', self.query,
                              re.DOTALL | re.IGNORECASE)
            if match is None:
                match = re.search(r'/\*.*(sortkey\s*\([^\()]*\)).*\**/', self.query,
                                  re.DOTALL | re.IGNORECASE)
            if match is not None:
                sortkey_stmt = match.group(1)
            else:
                sortkey_stmt = ''
            return sortkey_stmt
        else:
            return ''

    @property
    def unique_keys(self):
        match = re.search(r'/\*.*unique key\s*\(([^\()]*)\).*\**/', self.query,
                          re.DOTALL | re.IGNORECASE)
        if match is not None:
            unique_keys = [k.strip() for k in match.group(1).split(',')]
        else:
            unique_keys = []
        return unique_keys

    @property
    def create_view_stmt(self):
        return """
        DROP VIEW IF EXISTS {self.name} CASCADE;
        CREATE VIEW {self.name}
        AS
        {self.select_stmt};
        """.replace(' ' * 8, '').format(self=self)

    @property
    def create_table_stmt(self):
        if config.database_type == 'redshift':
            return """
            DROP TABLE IF EXISTS {self.name} CASCADE;
            CREATE TABLE {self.name} {self.distkey_stmt} {self.sortkey_stmt}
            AS
            {self.select_stmt};
            ANALYZE {self.name};
            """.replace(' ' * 8, '').format(self=self)
        else:
            return """
            DROP TABLE IF EXISTS {self.name} CASCADE;
            CREATE TABLE {self.name} {self.distkey_stmt} {self.sortkey_stmt}
            AS
            {self.select_stmt};
            """.replace(' ' * 8, '').format(self=self)


    @property
    def materialize_view_stmt(self):
        if config.database_type == 'redshift':
            return """
            CREATE SCHEMA IF NOT EXISTS {self.schema_prefix}{self.schema_name}{schema_suffix};
            DROP TABLE IF EXISTS {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name} CASCADE;
            CREATE TABLE {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name} {self.distkey_stmt} {self.sortkey_stmt}
            AS
            {self.select_stmt};
            ANALYZE {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name};
            DROP VIEW IF EXISTS {self.name} CASCADE;
            CREATE VIEW {self.name}
            AS
            SELECT * FROM {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name};
            """.replace(' ' * 8, '').format(self=self, schema_suffix='_mat')
        else:
            return """
            CREATE SCHEMA IF NOT EXISTS {self.schema_prefix}{self.schema_name}{schema_suffix};
            DROP TABLE IF EXISTS {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name} CASCADE;
            CREATE TABLE {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name} {self.distkey_stmt} {self.sortkey_stmt}
            AS
            {self.select_stmt};
            DROP VIEW IF EXISTS {self.name} CASCADE;
            CREATE VIEW {self.name}
            AS
            SELECT * FROM {self.schema_prefix}{self.schema_name}{schema_suffix}.{self.table_name};
            """.replace(' ' * 8, '').format(self=self, schema_suffix='_mat')