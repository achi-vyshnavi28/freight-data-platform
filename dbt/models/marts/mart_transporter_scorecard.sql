-- Monthly vendor review in one table: service (on time), honesty (billing), paperwork (invoice errors).
select
    transporter,
    count(*)                                                                   as shipments,
    count(*) filter (where status = 'DELIVERED')                               as delivered,
    round(100.0 * avg(case when on_time then 1 else 0 end) filter (where on_time is not null), 1) as on_time_pct,
    round(avg(delay_hours) filter (where on_time = false), 1)                  as avg_delay_hours_when_late,
    round(avg(stop_minutes), 0)                                                as avg_stop_minutes,
    count(*) filter (where is_overbilled)                                      as overbilled_invoices,
    round(100.0 * count(*) filter (where is_overbilled) / nullif(count(invoice_id), 0), 1) as overbilled_pct,
    coalesce(sum(freight_variance) filter (where is_overbilled), 0)            as overbilled_amount,
    count(*) filter (where invoice_arithmetic_ok = false or invoice_tax_type_ok = false) as invoices_with_errors,
    count(*) filter (where status = 'DELIVERED' and not is_invoiced)          as delivered_not_invoiced
from {{ ref('fct_shipments') }}
group by transporter
