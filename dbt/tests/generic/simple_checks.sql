{% test positive_or_null(model, column_name) %}
select * from {{ model }} where {{ column_name }} is not null and {{ column_name }} <= 0
{% endtest %}

{% test between(model, column_name, low, high) %}
select * from {{ model }} where {{ column_name }} is not null and ({{ column_name }} < {{ low }} or {{ column_name }} > {{ high }})
{% endtest %}
