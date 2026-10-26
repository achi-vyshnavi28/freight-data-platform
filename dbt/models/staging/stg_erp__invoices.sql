-- Latest revision of each invoice (the ERP edits invoices in place; we keep every extract in raw).
with ranked as (
    select
        *,
        row_number() over (partition by invoice_id order by updated_at desc, _batch_id desc) as revision_rank,
        count(*) over (partition by invoice_id) as revisions_seen
    from {{ source('raw', 'erp_invoices') }}
)

select
    invoice_id,
    shipment_id,
    trim(transporter)              as transporter,
    upper(trim(seller_gstin))      as seller_gstin,
    upper(trim(buyer_gstin))       as buyer_gstin,
    invoice_date::date             as invoice_date,
    freight_amount::numeric(12, 2) as freight_amount,
    other_charges::numeric(12, 2)  as other_charges,
    taxable_value::numeric(12, 2)  as taxable_value,
    cgst::numeric(12, 2)           as cgst,
    sgst::numeric(12, 2)           as sgst,
    igst::numeric(12, 2)           as igst,
    total_amount::numeric(12, 2)   as total_amount,
    updated_at,
    revisions_seen,
    abs(taxable_value + cgst + sgst + igst - total_amount) <= 1 as arithmetic_ok,
    -- Same state on both GSTINs = intra-state supply = CGST+SGST; otherwise IGST.
    case
        when left(seller_gstin, 2) = left(buyer_gstin, 2) then igst = 0 and cgst > 0
        else cgst = 0 and sgst = 0 and igst > 0
    end as tax_type_ok
from ranked
where revision_rank = 1
