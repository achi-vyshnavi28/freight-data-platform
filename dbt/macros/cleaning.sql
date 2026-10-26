{# Each transporter's TMS exports timestamps differently. Unknown formats become NULL and are quarantined. #}
{% macro parse_ts(col) -%}
case
    when {{ col }} ~ '^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$' then {{ col }}::timestamp
    when {{ col }} ~ '^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$' then to_timestamp({{ col }}, 'DD/MM/YYYY HH24:MI')::timestamp
    when {{ col }} ~ '^\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2} [AP]M$' then to_timestamp({{ col }}, 'DD-Mon-YYYY HH12:MI AM')::timestamp
end
{%- endmacro %}

{# "9800", "9800 kg", "9,800 KGS", "9.80 MT" -> kilograms #}
{% macro parse_weight_kg(col) -%}
case
    when {{ col }} ~* '(mt|ton)' then nullif(regexp_replace({{ col }}, '[^0-9.]', '', 'g'), '')::numeric * 1000
    else nullif(regexp_replace({{ col }}, '[^0-9.]', '', 'g'), '')::numeric
end
{%- endmacro %}

{% macro clean_plate(col) -%}
upper(regexp_replace({{ col }}, '[^A-Za-z0-9]', '', 'g'))
{%- endmacro %}

{% macro haversine_km(lat1, lon1, lat2, lon2) -%}
(2 * 6371.0088 * asin(sqrt(
    power(sin(radians({{ lat2 }} - {{ lat1 }}) / 2), 2)
    + cos(radians({{ lat1 }})) * cos(radians({{ lat2 }})) * power(sin(radians({{ lon2 }} - {{ lon1 }}) / 2), 2)
)))
{%- endmacro %}

{# GSTIN: format + mod-36 check character, in pure SQL. #}
{% macro gstin_is_valid(col) -%}
(
    {{ col }} ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'
    and substr({{ col }}, 15, 1) = substr(
        '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'::text,
        ((36 - (
            select sum(p / 36 + p % 36)
            from (
                select (strpos('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ', substr({{ col }}, i, 1)) - 1)
                       * (case when i % 2 = 0 then 2 else 1 end) as p
                from generate_series(1, 14) as i
            ) as weighted
        ) % 36) % 36)::int + 1,
        1
    )
)
{%- endmacro %}
