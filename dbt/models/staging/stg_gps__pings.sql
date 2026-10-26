{{
    config(
        materialized='incremental',
        unique_key='ping_id',
        incremental_strategy='delete+insert',
        indexes=[
            {'columns': ['vehicle_no', 'ts']},
            {'columns': ['ts'], 'type': 'brin'},
        ],
    )
}}

-- Deduplicated pings with a validity flag. Incremental: only new load batches are processed.
with source as (
    select * from {{ source('raw', 'gps_pings') }}
    {% if is_incremental() %}
    where _batch_id > (select coalesce(max(_batch_id), 0) from {{ this }})
    {% endif %}
),

deduped as (
    select distinct on (vehicle_no, ts)
        md5({{ clean_plate('vehicle_no') }} || '|' || ts) as ping_id,
        {{ clean_plate('vehicle_no') }}                    as vehicle_no,
        ts::timestamp                                      as ts,
        lat,
        lon,
        speed_kmph,
        _batch_id
    from source
    order by vehicle_no, ts, _batch_id desc
)

select
    *,
    case
        when lat = 0 and lon = 0 then 'null island fix'
        when lat not between 6 and 37 or lon not between 68 and 98 then 'outside India'
        when speed_kmph > 150 then 'speed spike'
    end as quality_issue
from deduped
