#!/bin/bash
# Purpose: Read CSV File and create harvest sources
# Author: James Brown
# ------------------------------------------

# Display help
help()
{
    echo "Adds harvest sources from a CSV file"
    echo
    echo "Syntax: harvest_additions [-f|h|k|o]"
    echo "options:"
    echo "[required] -f, --file             Path to CSV file"
    echo "-h, --help                        Print this help"
    echo "[required] -k, --api-key          CKAN API key"
    echo "[required] -o, --organization     CKAN organization name"
    echo "-u, --url                         Base URL for CKAN instance"
    echo
    exit 0
}

#Default value for testing
URL='localhost:8080'

#Argument parsing
POSITIONAL=()
while [[ $# -gt 0 ]]
do
key="$1"

case $key in
    -f|--file)
    FILE="$2"
    shift # past argument
    shift # past value
    ;;
    -h|--help)
    help
    shift # past argument
    ;;
    -k|--api-key)
    API_KEY="$2"
    shift # past argument
    shift # past value
    ;;
    -o|--organization)
    ORGANIZATION="$2"
    shift # past argument
    shift # past value
    ;;
    -u|--url)
    URL="$2"
    shift # past argument
    shift # past value
    ;;
    --default)
    DEFAULT=YES
    shift # past argument
    ;;
    *)    # unknown option
    POSITIONAL+=("$1") # save it in an array for later
    shift # past argument
    ;;
esac
done
set -- "${POSITIONAL[@]}" # restore positional parameters

# If arguments are not given/are not correct, exit
if [ ! -f "$FILE" ] || [ -z "$ORGANIZATION" ] || [ -z "$API_KEY" ]; then
    echo "Please specify the organization, API key, and file path. Use --help for more information."
    exit 99
fi

generate_harvest_data()
{
    cat <<EOF
{
    "title": "$title",
    "name": "$name",
    "url": "$waf_url",
    "owner_org": "$ORGANIZATION",
    "source_type": "waf",
    "config": "{\"private_datasets\": false}",
    "frequency": "$frequency"
}
EOF
}

OLDIFS=$IFS
IFS=','
while read title waf_url records records_in_error notes
do
    if [ $title != 'Title' ] 
    then
        name="${title,,}"
        name="${name// /-}"
        name="${name//\//-}"
        if [ $records -gt 3000 ]
        then
            frequency='MANUAL'
            echo "Setting frequency to manual due to records being greater than 3000..."
        else 
            frequency='WEEKLY'
        fi
        curl --location --request POST "$URL/api/3/action/harvest_source_create" \
            --header "Authorization: $API_KEY" \
            --header 'Content-Type: application/json' \
            --data-raw "$(generate_harvest_data $frequency)"
    fi
done < $FILE
IFS=$OLDIFS