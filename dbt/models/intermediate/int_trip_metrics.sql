{{ config(indexes=[{'columns': ['shipment_id'], 'unique': true}]) }}

-- Turn raw pings into per-trip facts: distance driven, stops (gaps-and-islands), GPS coverage.
with shipments as (
    select shipment_id, vehicle_no, dispatched_at, delivered_at
    from {{ ref('stg_tms__shipments') }}
    where quality_issue is null and dispatched_at is not null and delivered_at is not null
),

pings as (
    select s.shipment_id, p.ts, p.lat, p.lon, p.speed_kmph
    from shipments as s
    join {{ ref('stg_gps__pings') }} as p
      on p.vehicle_no = s.vehicle_no
     and p.ts between s.dispatched_at and s.delivered_at
    where p.quality_issue is null
),

sequenced as (
    select
        *,
        speed_kmph < 5 as is_stopped,
        lag(speed_kmph < 5) over w as prev_stopped,
        lag(lat) over w as prev_lat,
        lag(lon) over w as prev_lon,
        lag(ts) over w as prev_ts
    from pings
    window w as (partition by shipment_id order by ts)
),

islands as (
    -- a new island starts whenever the truck switches between moving and stopped
    select
        *,
        sum(case when is_stopped is distinct from prev_stopped then 1 else 0 end)
            over (partition by shipment_id order by ts) as island_id
    from sequenced
),

stops as (
    select shipment_id, island_id, extract(epoch from max(ts) - min(ts)) / 60 as stop_minutes
    from islands
    where is_stopped
    group by shipment_id, island_id
),

stop_summary as (
    select
        shipment_id,
        sum(stop_minutes)                               as stop_minutes,
        count(*) filter (where stop_minutes >= 60)      as long_stops,
        max(stop_minutes)                               as longest_stop_minutes
    from stops
    group by shipment_id
),

movement as (
    select
        shipment_id,
        count(*)                                                                   as ping_count,
        min(ts)                                                                    as first_ping_at,
        max(ts)                                                                    as last_ping_at,
        sum({{ haversine_km('prev_lat', 'prev_lon', 'lat', 'lon') }})              as gps_distance_km,
        max(speed_kmph)                                                            as max_speed_kmph,
        max(extract(epoch from ts - prev_ts)) / 60                                 as max_ping_gap_minutes
    from sequenced
    group by shipment_id
)

select
    m.shipment_id,
    m.ping_count,
    m.first_ping_at,
    m.last_ping_at,
    round(m.gps_distance_km::numeric, 1)            as gps_distance_km,
    m.max_speed_kmph,
    round(m.max_ping_gap_minutes::numeric, 1)       as max_ping_gap_minutes,
    round(coalesce(s.stop_minutes, 0)::numeric, 1)  as stop_minutes,
    coalesce(s.long_stops, 0)                       as long_stops,
    round(coalesce(s.longest_stop_minutes, 0)::numeric, 1) as longest_stop_minutes
from movement as m
left join stop_summary as s using (shipment_id)
