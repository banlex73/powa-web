from sqlalchemy.sql import (select, cast, func, column, text, extract, case,
                            bindparam, literal_column)
from sqlalchemy.types import Numeric
from sqlalchemy.sql.functions import max, min, sum
from powa.sql.utils import *


def powa_base_statdata_detailed_db():
    base_query = text("""
    pg_database,
    LATERAL
    (
        SELECT unnested.dbid, unnested.queryid,(unnested.records).*
        FROM (
            SELECT psh.dbid, psh.queryid, psh.coalesce_range, unnest(records) AS records
            FROM powa_statements_history psh
            WHERE coalesce_range && tstzrange(:from, :to, '[]')
            AND psh.dbid = pg_database.oid
            AND psh.queryid IN ( SELECT powa_statements.queryid FROM powa_statements WHERE powa_statements.dbid = pg_database.oid )
        ) AS unnested
        WHERE tstzrange(:from, :to, '[]') @> (records).ts
        UNION ALL
        SELECT psc.dbid, psc.queryid,(psc.record).*
        FROM powa_statements_history_current psc
        WHERE tstzrange(:from,:to,'[]') @> (record).ts
        AND psc.dbid = pg_database.oid
        AND psc.queryid IN ( SELECT powa_statements.queryid FROM powa_statements WHERE powa_statements.dbid = pg_database.oid )
    ) h
    """)
    return base_query

def powa_base_statdata_db():
    base_query = text("""(
          SELECT dbid, min(lower(coalesce_range)) AS min_ts, max(upper(coalesce_range)) AS max_ts
          FROM powa_statements_history_db dbh
          JOIN pg_database ON dbh.dbid = pg_database.oid
          WHERE coalesce_range && tstzrange(:from, :to, '[]')
          GROUP BY dbid
    ) ranges,
    LATERAL (
        SELECT (unnested1.records).*
        FROM (
            SELECT dbh.coalesce_range, unnest(records) AS records
            FROM powa_statements_history_db dbh
            WHERE coalesce_range @> min_ts
            AND dbh.dbid = ranges.dbid
        ) AS unnested1
        WHERE tstzrange(:from, :to, '[]') @> (unnested1.records).ts
        UNION ALL
        SELECT (unnested2.records).*
        FROM (
            SELECT dbh.coalesce_range, unnest(records) AS records
            FROM powa_statements_history_db dbh
            WHERE coalesce_range @> max_ts
            AND dbh.dbid = ranges.dbid
        ) AS unnested2
        WHERE tstzrange(:from, :to, '[]') @> (unnested2.records).ts
        UNION ALL
        SELECT (dbc.record).*
        FROM powa_statements_history_current_db dbc
        WHERE tstzrange(:from, :to, '[]') @> (dbc.record).ts
        AND dbc.dbid = ranges.dbid
    ) AS db_history
    """)
    return base_query

def get_diffs_forstatdata():
    return [
        diff("calls"),
        diff("total_time").label("runtime"),
        diff("shared_blks_read"),
        diff("shared_blks_hit"),
        diff("shared_blks_dirtied"),
        diff("shared_blks_written"),
        diff("temp_blks_read"),
        diff("temp_blks_written"),
        diff("blk_read_time"),
        diff("blk_write_time")
    ]

def powa_getstatdata_detailed_db():
    base_query = powa_base_statdata_detailed_db()
    diffs = get_diffs_forstatdata()
    return (select([
        column("queryid"),
        column("dbid"),
        column("datname"),
] + diffs)
        .select_from(base_query)
        .group_by(column("queryid"), column("dbid"), column("datname"))
        .having(max(column("calls")) - min(column("calls")) > 0))

def powa_getstatdata_db():
    base_query = powa_base_statdata_db()
    diffs = get_diffs_forstatdata()
    return (select([column("dbid")] + diffs)
            .select_from(base_query)
            .group_by(column("dbid"))
            .having(max(column("calls")) - min(column("calls")) > 0))


BASE_QUERY_SAMPLE_DB = text("""(
    SELECT datname, base.* FROM pg_database,
    LATERAL (
        SELECT *
        FROM (
            SELECT
            row_number() OVER (PARTITION BY dbid ORDER BY statements_history.ts) AS number,
            count(*) OVER (PARTITION BY dbid) AS total,
            *
            FROM (
                SELECT dbid, (unnested.records).*
                FROM (
                    SELECT psh.dbid, psh.coalesce_range, unnest(records) AS records
                    FROM powa_statements_history_db psh
                    WHERE coalesce_range && tstzrange(:from, :to,'[]')
                    AND psh.dbid = pg_database.oid
                ) AS unnested
                WHERE tstzrange(:from, :to, '[]') @> (records).ts
                UNION ALL
                SELECT dbid, (record).*
                FROM powa_statements_history_current_db
                WHERE tstzrange(:from, :to, '[]') @> (record).ts
                AND dbid = pg_database.oid
            ) AS statements_history
        ) AS sh
        WHERE number % ( int8larger((total)/(:samples+1),1) ) = 0
    ) AS base
) AS by_db
""")

BASE_QUERY_SAMPLE = text("""(
    SELECT datname, dbid, queryid, base.*
    FROM powa_statements JOIN pg_database ON pg_database.oid = powa_statements.dbid,
    LATERAL (
        SELECT *
        FROM (SELECT
            row_number() OVER (PARTITION BY queryid ORDER BY statements_history.ts) AS number,
            count(*) OVER (PARTITION BY queryid) AS total,
            *
            FROM (
                SELECT (unnested.records).*
                FROM (
                    SELECT psh.queryid, psh.coalesce_range, unnest(records) AS records
                    FROM powa_statements_history psh
                    WHERE coalesce_range && tstzrange(:from, :to, '[]')
                    AND psh.queryid = powa_statements.queryid
                ) AS unnested
                WHERE tstzrange(:from, :to, '[]') @> (records).ts
                UNION ALL
                SELECT (record).*
                FROM powa_statements_history_current phc
                WHERE tstzrange(:from, :to, '[]') @> (record).ts
                AND phc.queryid = powa_statements.queryid
            ) AS statements_history
        ) AS sh
        WHERE number % ( int8larger((total)/(:samples+1),1) ) = 0
    ) AS base
) AS by_query
""")


def powa_getstatdata_sample(mode):
    if mode == "db":
        base_query = BASE_QUERY_SAMPLE_DB
        base_columns = ["dbid"]

    elif mode == "query":
        base_query = BASE_QUERY_SAMPLE
        base_columns = ["dbid", "queryid"]


    def biggest(var, minval=0, label=None):
        label = label or var
        return func.greatest(
            func.lead(column(var)).over(order_by="ts", partition_by=base_columns) - column(var),
            minval).label(label)

    return select(base_columns + [
        "ts",
        biggest("ts", '0 s', "mesure_interval"),
        biggest("calls"),
        biggest("total_time", label="runtime"),
        biggest("rows"),
        biggest("shared_blks_read"),
        biggest("shared_blks_hit"),
        biggest("shared_blks_dirtied"),
        biggest("shared_blks_written"),
        biggest("local_blks_read"),
        biggest("local_blks_hit"),
        biggest("local_blks_dirtied"),
        biggest("local_blks_written"),
        biggest("temp_blks_read"),
        biggest("temp_blks_written"),
        biggest("blk_read_time"),
        biggest("blk_write_time")]).select_from(base_query)



BASE_QUERY_QUALSTATS_SAMPLE = text("""
powa_statements ps
JOIN powa_qualstats_statements pqs USING(md5query)
JOIN powa_qualstats_nodehash nh USING(queryid)
, LATERAL (
    SELECT  sh.ts,
         sh.nodehash,
         sh.quals,
         int8larger(lead(sh.count) over (querygroup) - sh.count,0) count,
         CASE WHEN sum(sh.count) over querygroup > 0 THEN sum(sh.count * sh.filter_ratio) over (querygroup) / sum(sh.count) over (querygroup) ELSE 0 END as filter_ratio
         FROM (
      SELECT * FROM
      (
         SELECT row_number() over (order by quals_history.ts) as number, *,
          count(*) OVER () as total
         FROM (
          SELECT unnested.queryid, unnested.nodehash, (unnested.records).*
          FROM (
              SELECT nh.queryid, nh.nodehash, nh.coalesce_range, unnest(records) AS records
              FROM powa_qualstats_nodehash_history nh
              WHERE coalesce_range && tstzrange(:from, :to)
              AND queryid = nh.queryid AND nodehash = nh.nodehash
          ) AS unnested
          WHERE tstzrange(:from, :to) @> (records).ts and queryid = nh.queryid
          UNION ALL
          SELECT powa_qualstats_nodehash_current.queryid, powa_qualstats_nodehash_current.nodehash, powa_qualstats_nodehash_current.ts, powa_qualstats_nodehash_current.quals, powa_qualstats_nodehash_current.avg_filter_ratio, powa_qualstats_nodehash_current.count
          FROM powa_qualstats_nodehash_current
          WHERE tstzrange(:from, :to)@> powa_qualstats_nodehash_current.ts
          AND queryid = nh.queryid AND nodehash = nh.nodehash
        ) quals_history
     ) numbered_history WHERE number % (int8larger(total/(:samples+1),1) )=0
    ) sh
     WINDOW querygroup AS (PARTITION BY sh.nodehash  ORDER BY sh.ts)
) samples
""")

def qualstat_getstatdata_sample():
    base_query = BASE_QUERY_QUALSTATS_SAMPLE
    base_columns = [
        literal_column("nh.queryid").label("queryid"),
        func.to_json(literal_column("nh.quals")).label("quals"),
        literal_column("nh.nodehash").label("nodehash"),
        "count",
        "filter_ratio",
        "md5query"]
    return (select(base_columns)
            .select_from(base_query)
            .where(column("count") != None))


def qualstat_base_statdata():
    base_query = text("""
    (
    SELECT unnested.nodehash, unnested.queryid,  (unnested.records).*
    FROM (
        SELECT pqnh.nodehash, pqnh.queryid, pqnh.coalesce_range, unnest(records) as records
        FROM powa_qualstats_nodehash_history pqnh
        WHERE coalesce_range && tstzrange(:from, :to, '[]')
        AND pqnh.queryid IN ( SELECT pqs.queryid FROM powa_qualstats_statements pqs WHERE pqs.md5query = :query)
    ) AS unnested
    WHERE tstzrange(:from, :to, '[]') @> (records).ts
    UNION ALL
        SELECT pqnc.nodehash, pqnc.queryid, pqnc.ts, pqnc.quals, pqnc.avg_filter_ratio, pqnc.count
        FROM powa_qualstats_nodehash_current pqnc
        WHERE tstzrange(:from, :to, '[]') @> pqnc.ts
        AND pqnc.queryid IN ( SELECT pqs.queryid FROM powa_qualstats_statements pqs WHERE pqs.md5query = :query)
    ) h JOIN powa_qualstats_statements USING (queryid)
    """)
    return base_query


def qualstat_getstatdata():
    base_query = qualstat_base_statdata()
    return (select([
        column("nodehash"),
        column("queryid"),
        column("md5query"),
        func.to_json(column("quals")).label("quals"),
        diff("count"),
        (sum(column("count") * column("filter_ratio")) /
         sum(column("count"))).label("filter_ratio")])
        .select_from(base_query)
        .group_by(column("nodehash"), column("queryid"), column("quals"), column("md5query"))
        .having(max(column("count")) - min(column("count")) > 0))
