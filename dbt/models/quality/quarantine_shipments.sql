-- Rows staging refused to pass downstream, with the reason. Nothing is dropped silently.
select shipment_id, transporter, origin, destination, status, quality_issue
from {{ ref('stg_tms__shipments') }}
where quality_issue is not null
