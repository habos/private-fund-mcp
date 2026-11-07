create temp table base as
 select 'RIA' status, FilingID, "1E1" crd, "1A" legal_name, "1B1" biz_name, "1F1-City" city, "1F1-State" state, "1F1-Country" country, "5F2c" raum,
   coalesce(try_strptime(DateSubmitted, '%m/%d/%Y %I:%M:%S %p'), try_strptime(DateSubmitted, '%m/%d/%Y')) submitted, month from IA_BASE
 union all
 select 'ERA', FilingID, "1E1", "1A", "1B1", "1F1-City", "1F1-State", "1F1-Country", null, coalesce(try_strptime(DateSubmitted, '%m/%d/%Y %I:%M:%S %p'), try_strptime(DateSubmitted, '%m/%d/%Y')), month from ERA_BASE;
-- one row per filing, prefer the RIA copy when a filing appears in both files
create temp table filing as select * from base qualify row_number() over (partition by FilingID order by status='RIA' desc) = 1;
create temp table latest as select * from filing qualify row_number() over (partition by crd order by submitted desc, FilingID::bigint desc) = 1;
create temp table b1 as select * from (select 'IA' src, * from IA_7B1 union all by name select 'ERA' src, * from ERA_7B1)
  qualify row_number() over (partition by FilingID, ReferenceID order by src='IA' desc) = 1;
create temp table a22 as select distinct FilingID, ReferenceID, "Form D File Number" file_no from (select * from IA_7B1A22 union all select * from ERA_7B1A22);
create temp table a23 as select * from (select * from IA_7B1A23 union all by name select * from ERA_7B1A23);
create temp table inscope_funds as
  select l.crd, l.status, l.submitted, b1.* from latest l join b1 using (FilingID)
  where b1."Fund Type" in ('Venture Capital Fund', 'Private Equity Fund');
