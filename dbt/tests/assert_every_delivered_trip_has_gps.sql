-- A delivered shipment with no GPS means the telematics feed (or the vehicle mapping) is broken.
{{ config(severity='warn', warn_if='>0', error_if='>10') }}
select shipment_id, vehicle_no
from {{ ref('fct_shipments') }}
where status = 'DELIVERED' and coalesce(ping_count, 0) = 0
