"""
Utilities for commonly used SQL constructs.
"""
import re
from sqlalchemy.sql import (text, select, func, case, column, extract,
                            cast, bindparam, and_, literal_column)
from sqlalchemy.sql.operators import op
from sqlalchemy.types import Numeric
from sqlalchemy.dialects.postgresql import array, dialect as pgdialect
from collections import namedtuple, defaultdict
from powa.json import JSONizable

TOTAL_MEASURE_INTERVAL = """
extract( epoch from
    CASE WHEN min(total_mesure_interval) = '0 second'
        THEN '1 second'::interval
    ELSE min(total_mesure_interval) END)
"""


def format_jumbled_query(sql, params):
    it = iter(params)
    to_replace = re.findall("\?", sql)
    result = None
    try:
        sql = re.sub("\?", lambda val: next(it), sql)
    except StopIteration:
        pass
    return result


RESOLVE_OPNAME = text("""
SELECT json_object_agg(oid, value)
    FROM (

    SELECT oproid as oid, json_build_object(
        'name', oprname,
        'amop',
        coalesce(
            json_object_agg(am, opclass_oids::jsonb
            ORDER BY am)
                      FILTER (WHERE am is NOT NULL)),
        'amop_names',
         coalesce(
            json_object_agg(
                      amname,
                      opclass_names ORDER BY am)
                      FILTER (WHERE am is NOT NULL))) as value
    FROM
    (
        SELECT oprname, pg_operator.oid as oproid,
            pg_am.oid as am, to_json(array_agg(distinct c.oid)) as opclass_oids,
            amname,
        to_json(array_agg(distinct CASE WHEN opcdefault THEN '' ELSE opcname END)) as opclass_names
        FROM
        pg_operator
        LEFT JOIN pg_amop amop ON amop.amopopr = pg_operator.oid
        LEFT JOIN pg_am ON amop.amopmethod = pg_am.oid AND pg_am.amname != 'hash'
        LEFt JOIN pg_opfamily f ON f.opfmethod = pg_am.oid AND amop.amopfamily = f.oid
        LEFT JOIN pg_opclass c ON c.opcfamily = f.oid
        WHERE pg_operator.oid in :oid_list
        GROUP BY pg_operator.oid, oprname, pg_am.oid, amname
    ) by_am
    GROUP BY oproid, oprname
    ) detail
""")

RESOLVE_ATTNAME = text("""
    SELECT json_object_agg(attrelid || '.'|| attnum, value)
    FROM (
    SELECT attrelid, attnum, json_build_object(
        'relname', relname,
        'attname', attname,
        'nspname', nspname,
        'n_distinct', stadistinct,
        'null_frac', stanullfrac,
        'most_common_values', CASE
            WHEN s.stakind1 = 1 THEN s.stavalues1
            WHEN s.stakind2 = 1 THEN s.stavalues2
            WHEN s.stakind3 = 1 THEN s.stavalues3
            WHEN s.stakind4 = 1 THEN s.stavalues4
            WHEN s.stakind5 = 1 THEN s.stavalues5
            ELSE NULL::anyarray
        END,
        'table_liverows', pg_stat_get_live_tuples(c.oid)
    ) as value
    FROM pg_attribute a
    INNER JOIN pg_class c on c.oid = a.attrelid
    INNER JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_statistic s ON s.starelid = c.oid
                       AND s.staattnum = a.attnum
    WHERE (attrelid, attnum) IN :att_list
    ) detail
""")


class ResolvedQual(JSONizable):

    def __init__(self, nspname, relname, attname,
                 opname, amops,
                 n_distinct=None,
                 most_common_values=None,
                 null_frac=None,
                 example_values=None,
                 eval_type=None,
                 relid=None,
                 attnum=None):
        self.nspname = nspname
        self.relname = relname
        self.attname = attname
        self.opname = opname
        self.amops = amops
        self.n_distinct = n_distinct
        self.most_common_values = most_common_values
        self.null_frac = null_frac
        self.example_values = example_values or []
        self.eval_type = eval_type
        self.relid = relid
        self.attnum = attnum

    def __str__(self):
        return "%s.%s %s ?" % (self.relname, self.attname, self.opname)

    @property
    def amop_keys(self):
        return [" ".join(amop) for amop in self.amops]

    @property
    def distinct_values(self):
        if self.n_distinct > 0:
            return "%s" % self.n_distinct
        else:
            return "%s %%" % (abs(self.n_distinct) * 100)

    def to_json(self):
        base = super(ResolvedQual, self).to_json()
        base['amop_keys'] = self.amop_keys
        return base


class ComposedQual(JSONizable):

    def __init__(self, nspname=None, relname=None,
                 nbfiltered=None,
                 filter_ratio = None,
                 count=None,
                 table_liverows=None,
                 qualid=None,
                 relid=None):
        super(ComposedQual, self).__init__()
        self.qualid = qualid
        self.relname = relname
        self.nspname = nspname
        self.nbfiltered = nbfiltered
        self.filter_ratio = filter_ratio
        self.count = count
        self.table_liverows = table_liverows
        self.relid = relid
        self._quals = []

    def append(self, element):
        if not isinstance(element, ResolvedQual):
            raise ValueError(("ComposedQual elements must be instances of ",
                             "ResolvedQual"))
        self._quals.append(element)

    def __iter__(self):
        return self._quals.__iter__()

    def __str__(self):
        return " AND ".join(str(v) for v in self._quals)

    @property
    def where_clause(self):
        return "WHERE %s" % self

    def to_json(self):
        base = super(ComposedQual, self).to_json()
        base['quals'] = self._quals
        base['where_clause'] = self.where_clause
        return base



def resolve_quals(conn, quallist, attribute="quals"):
    """
    Resolve quals definition (as dictionary coming from a to_json(quals)
    sql query.

    Arguments:
        conn: a connection to the database against which the qual was executed
        quallist: an iterable of rows, each storing quals in the attributes
        attribute: the attribute containing the qual list itself in each row
    Returns:
        a list of ComposedQual objects
    """
    operator_to_look = set()
    attname_to_look = set()
    operators = {}
    attnames = {}
    for row in quallist:
        values = row[attribute]
        if not isinstance(values, list):
            values = [values]
        for v in values:
            operator_to_look.add(v['opno'])
            attname_to_look.add((v["relid"], v["attnum"]))
    if operator_to_look:
        operators = conn.execute(
            RESOLVE_OPNAME,
            {"oid_list": tuple(operator_to_look)}).scalar()
    if attname_to_look:
        attnames = conn.execute(
            RESOLVE_ATTNAME,
            {"att_list": tuple(attname_to_look)}).scalar()
    new_qual_list = []
    for row in quallist:
        row = dict(row)
        newqual = ComposedQual(
            count=row['count'],
            nbfiltered=row['nbfiltered'],
            filter_ratio=row['filter_ratio'],
            qualid=row['qualid']
        )
        new_qual_list.append(newqual)
        values = [v for v in row[attribute] if v['relid'] != '0']
        if not isinstance(values, list):
            values = [values]
        for v in values:
            attname = attnames["%s.%s" % (v["relid"], v["attnum"])]
            if newqual.relname is not None:
                if newqual.relname != attname['relname']:
                    raise ValueError("All individual qual parts should be on the "
                                     "same relation")
            else:
                newqual.relname = attname["relname"]
                newqual.nspname = attname["nspname"]
                newqual.relid = v["relid"]
                newqual.table_liverows = attname["table_liverows"]
            newqual.append(ResolvedQual(
                nspname=attname['nspname'],
                relname=attname['relname'],
                attname=attname['attname'],
                opname=operators[v["opno"]]["name"],
                amops=operators[v["opno"]]["amop_names"],
                n_distinct=attname["n_distinct"],
                most_common_values=attname["most_common_values"],
                null_frac=attname["null_frac"],
                eval_type=v["eval_type"],
                relid=v["relid"],
                attnum=v["attnum"]))
    return new_qual_list


Plan = namedtuple(
    "Plan",
    ("title", "values", "query", "plan", "filter_ratio", "exec_count"))


def qual_constants(type, filter_clause, top=1):
    orders = {
        'most_executed': "4 DESC",
        'least_filtering': "6",
        'most_filtering': "6 DESC"
    }
    if type not in ('most_executed', 'most_filtering',
                    'least_filtering'):
        return
    dialect = pgdialect()
    dialect.paramstyle = 'named'
    filter_clause = filter_clause.compile(dialect=dialect)
    base = text("""
    (
    WITH sample AS (
    SELECT query, s.queryid, qn.qualid, quals as quals,
                constants,
                sum(count) as count,
                sum(nbfiltered) as nbfiltered,
                CASE WHEN sum(count) = 0 THEN 0 ELSE sum(nbfiltered) / sum(count) END AS filter_ratio
        FROM powa_statements s
        JOIN pg_database ON pg_database.oid = s.dbid
        JOIN powa_qualstats_quals qn ON s.queryid = qn.queryid
        JOIN (
            SELECT *
            FROM powa_qualstats_constvalues_history qnc
            UNION ALL
            SELECT *
            FROM powa_qualstats_aggregate_constvalues_current
        ) qnc ON qn.qualid = qnc.qualid AND qn.queryid = qnc.queryid,
        LATERAL
                unnest(%s) as t(constants,nbfiltered,count)
        WHERE %s
        GROUP BY qn.qualid, quals, constants, s.queryid, query
        ORDER BY %s
        LIMIT :top_value
    )
    SELECT query, queryid, qualid, quals, constants as constants, nbfiltered as nbfiltered,
                count as count,
                filter_ratio as filter_ratio,
                row_number() OVER (ORDER BY count desc NULLS LAST) as rownumber
        FROM sample
    ORDER BY 9
    LIMIT :top_value
    ) %s
    """ % (type, str(filter_clause), orders[type], type)
                )
    base = base.params(top_value=top, **filter_clause.params)
    return select(["*"]).select_from(base)

def quote_ident(name):
    return '"' + name + '"'

def get_plans(self, query, database, qual):
    plans = []
    for key in ('most filtering', 'least filtering', 'most executed'):
        vals = qual[key]
        query = format_jumbled_query(query, vals['constants'])
        plan = "N/A"
        try:
            result = self.execute("EXPLAIN %s" % query,
                                    database=database)
            plan = "\n".join(v[0] for v in result)
        except:
            pass
        plans.append(Plan(key, vals['constants'], query,
                            plan, vals["filter_ratio"], vals['count']))
    return plans


def get_sample_query(ctrl, database, queryid, _from, _to):
    """
    From a queryid, build a query string ready to be executed.

    If the only constants in the query are in the predicates, use values from
    the most_executed quals. If not, use the query_example collected by
    pg_qualstats, as is.
    """
    has_pgqs = ctrl.has_extension("pg_qualstats")
    normalized_query = None
    example_query = None
    if has_pgqs and has_pgqs >= "0.0.7":
        rs = list(ctrl.execute(text("""
            SELECT query, pg_qualstats_example_query(queryid)
            FROM powa_statements
            WHERE queryid = :queryid
            LIMIT 1
        """), params={"queryid": queryid}))[0]
        normalized_query = rs[0]
        example_query = rs[1]
    else:
        rs = list(ctrl.execute(text("""
            SELECT query FROM powa_statements WHERE queryid = :queryid LIMIT 1
        """), params={"queryid": queryid}))[0]
        normalized_query = rs[0]
        example_query = None
    values = qualstat_get_figures(ctrl, database, _from, _to,
                                  queries=[queryid])

    if values is None:
        values = {'most executed': {}}
        # Try to inject values
    sql = format_jumbled_query(normalized_query,
                                values['most executed'].get('constants', []))
    return sql or example_query


def qualstat_get_figures(conn, database, tsfrom, tsto, queries=None, quals=None):
    condition = text("""datname = :database AND coalesce_range && tstzrange(:from, :to)""")
    if queries is not None:
        condition = and_(condition, array([int(q) for q in queries])
                         .any(literal_column("s.queryid")))
    if quals is not None:
        condition = and_(condition, array([int(q) for q in quals])
                         .any(literal_column("qnc.qualid")))
    sql = (select([
                  text('most_filtering.quals'),
                  text('most_filtering.query'),
                  text('to_json(most_filtering) as "most filtering"'),
                  text('to_json(least_filtering) as "least filtering"'),
                  text('to_json(most_executed) as "most executed"')])
           .select_from(
               qual_constants("most_filtering", condition)
               .alias("most_filtering")
               .join(
                   qual_constants("least_filtering", condition)
                   .alias("least_filtering"),
                   text("most_filtering.rownumber = "
                        "least_filtering.rownumber"))
               .join(qual_constants("most_executed", condition)
                     .alias("most_executed"),
                     text("most_executed.rownumber = "
                          "least_filtering.rownumber"))))

    params = {"database": database,
              "from": tsfrom,
              "to": tsto}
    quals = conn.execute(sql, params=params)

    if quals.rowcount == 0:
        return None

    row = quals.first()

    return row


class HypoPlan(JSONizable):

    def __init__(self, baseplan, basecost,
                 hypoplan, hypocost,
                 query, indexes=None):
        self.baseplan = baseplan
        self.basecost = basecost
        self.hypoplan = hypoplan
        self.hypocost = hypocost
        self.query = query
        self.indexes = indexes or []

    @property
    def gain_percent(self):
        return round(100 - float(self.hypocost) * 100 / float(self.basecost), 2)

    def to_json(self):
        base = super(HypoPlan, self).to_json()
        base['gain_percent'] = self.gain_percent
        return base

class HypoIndex(JSONizable):

    def __init__(self, nspname, relname, amname,
                 composed_qual=None):
        self.nspname = nspname
        self.relname = relname
        self.qual = composed_qual
        self.amname = amname
        self.name = None

    @property
    def ddl(self):
        # Only btree is supported right now
        if 'btree' == self.amname:
            attrs = []
            for qual in self.qual:
                if qual.attname not in attrs:
                    attrs.append(qual.attname)
            return ("""CREATE INDEX ON %s.%s(%s)""" % (
                quote_ident(self.nspname),
                quote_ident(self.relname),
                ",".join(attrs)))

    @property
    def hypo_ddl(self):
        ddl = self.ddl
        if ddl is not None:
            return func.hypopg_create_index(self.ddl)

    def to_json(self):
        base = super(HypoIndex, self).to_json()
        base['ddl'] = self.ddl
        return base


def possible_indexes(composed_qual):
    by_am = defaultdict(list)
    for qual in composed_qual:
        for am in qual.amops.keys():
            by_am[am].append(qual)
    indexes = []
    for am, quals in by_am.items():
        base = quals[0]
        indexes.append(HypoIndex(base.nspname,
                                 base.relname,
                                 am,
                                 quals))
    return indexes


def get_hypoplans(conn, query, indexes=None):
    """
    With a connection to a database where hypothetical indexes
    have already been created, request two plans for each query:
        - one with hypothetical indexes
        - one without hypothetical indexes

    Arguments:
        conn: a connection to the target database
        queries: a list of sql queries, already formatted with values
        indexes: a list of HypoIndex to look for in the plan. They should have been created, and have a name.
    """
    indexes = indexes or []
    # Escape literal '%'
    query = query.replace("%", "%%")
    with conn.begin() as trans:
        trans.execute("SET hypopg.enabled = off")
        baseplan = "\n".join(v[0] for v in trans.execute("EXPLAIN %s" % query))
        trans.execute("SET hypopg.enabled = on")
        hypoplan = "\n".join(v[0] for v in trans.execute("EXPLAIN %s" % query))
    COST_RE = "(?<=\.\.)\d+\.\d+"
    m = re.search(COST_RE, baseplan)
    basecost = float(m.group(0))
    m = re.search(COST_RE, hypoplan)
    hypocost = float(m.group(0))
    used_indexes = []
    for ind in indexes:
        if ind.name is None:
            continue
        if ind.name in hypoplan:
            used_indexes.append(ind)
    return HypoPlan(baseplan, basecost, hypoplan, hypocost, query, used_indexes)
