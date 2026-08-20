#!/bin/bash

# Set the path to the Python script file
PYTHON_FILE="src/component.py"
# Set the path to the Markdown file containing actions
MD_FILE="component_config/actions.md"

# Check if the file exists before creating it
if [ ! -e "$MD_FILE" ]; then
    touch "$MD_FILE"
else
    echo "File already exists: $MD_FILE"
    exit 1
fi

# Get all occurrences of lines containing @sync_action('XXX') from the .py file
SYNC_ACTIONS=$(grep -o -E "@sync_action\(['\"][^'\"]*['\"]\)" "$PYTHON_FILE" | sed "s/@sync_action(\(['\"]\)\([^'\"]*\)\(['\"]\))/\2/" | sort | uniq)

# Check if any sync actions were found
if [ -n "$SYNC_ACTIONS" ]; then
    # Iterate over each occurrence of @sync_action('XXX')
    for sync_action in $SYNC_ACTIONS; do
        EXISTING_ACTIONS+=("$sync_action")
    done

    # Convert the array to JSON format
    JSON_ACTIONS=$(printf '"%s",' "${EXISTING_ACTIONS[@]}")
    JSON_ACTIONS="[${JSON_ACTIONS%,}]"

    # Update the content of the actions.md file
    echo "$JSON_ACTIONS" > "$MD_FILE"
else
    echo "No sync actions found. Not creating the file."
fi