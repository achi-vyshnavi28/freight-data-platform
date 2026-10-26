select quality_issue, count(*) as pings, min(ts) as first_seen, max(ts) as last_seen
from {{ ref('stg_gps__pings') }}
where quality_issue is not null
group by quality_issue
