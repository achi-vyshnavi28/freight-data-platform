-- If more than 2% of shipments fail parsing, a source changed its format: stop the pipeline.
select quarantined, total
from (
    select count(*) filter (where quality_issue is not null) as quarantined, count(*) as total
    from {{ ref('stg_tms__shipments') }}
) as t
where quarantined > 0.02 * total
