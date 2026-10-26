-- GPS distance far from the lane distance means pings were attributed to the wrong trip.
{{ config(severity='warn') }}
select f.shipment_id, f.gps_distance_km, l.road_km_estimate
from {{ ref('fct_shipments') }} as f
join {{ ref('dim_lane') }} as l using (lane)
where f.ping_count > 50
  and (f.gps_distance_km < 0.5 * l.road_km_estimate or f.gps_distance_km > 1.5 * l.road_km_estimate)
