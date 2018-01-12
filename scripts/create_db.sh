##
## crete OTT spatial db for OTT
##

mac_psql=/Applications/Postgres.app/Contents/Versions/9.4/bin/psql
unix_psql=`which psql`

if [ -f "$mac_psql" ]
then
    psql=$mac_psql
elif [ -f "$unix_psql" ]
then
    psql=$unix_psql
fi

def_db=postgres
user=ott
db=ott

$psql -d $def_db -c "CREATE USER ${user};"
$psql -d $def_db -c "CREATE DATABASE ${db} WITH OWNER ${user};"
$psql -d $db -c "CREATE EXTENSION postgis;"



