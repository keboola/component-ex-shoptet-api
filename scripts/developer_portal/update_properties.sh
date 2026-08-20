#!/usr/bin/env bash

set -e

# Check if the KBC_DEVELOPERPORTAL_APP environment variable is set
if [ -z "$KBC_DEVELOPERPORTAL_APP" ]; then
    echo "Error: KBC_DEVELOPERPORTAL_APP environment variable is not set."
    exit 1
fi

# Pull the latest version of the developer portal CLI Docker image
docker pull quay.io/keboola/developer-portal-cli-v2:latest

# Function to update a property for the given app ID
update_property() {
    local app_id="$1"
    local prop_name="$2"
    local file_path="$3"

    if [ ! -f "$file_path" ]; then
        echo "File '$file_path' not found. Skipping update for property '$prop_name' of application '$app_id'."
        return
    fi

    # shellcheck disable=SC2155
    local value=$(<"$file_path")

    echo "Updating $prop_name for $app_id"
    echo "$value"

    if [ -n "$value" ]; then
        docker run --rm \
            -e KBC_DEVELOPERPORTAL_USERNAME \
            -e KBC_DEVELOPERPORTAL_PASSWORD \
            quay.io/keboola/developer-portal-cli-v2:latest \
            update-app-property "$KBC_DEVELOPERPORTAL_VENDOR" "$app_id" "$prop_name" --value="$value"
        echo "Property $prop_name updated successfully for $app_id"
    else
        echo "$prop_name is empty for $app_id, skipping..."
    fi
}

app_id="$KBC_DEVELOPERPORTAL_APP"

update_property "$app_id" "isDeployReady" "component_config/isDeployReady.md"
update_property "$app_id" "longDescription" "component_config/component_long_description.md"
update_property "$app_id" "configurationSchema" "component_config/configSchema.json"
update_property "$app_id" "configurationRowSchema" "component_config/configRowSchema.json"
update_property "$app_id" "configurationDescription" "component_config/configuration_description.md"
update_property "$app_id" "shortDescription" "component_config/component_short_description.md"
update_property "$app_id" "logger" "component_config/logger"
update_property "$app_id" "loggerConfiguration" "component_config/loggerConfiguration.json"
update_property "$app_id" "licenseUrl" "component_config/licenseUrl.md"
update_property "$app_id" "documentationUrl" "component_config/documentationUrl.md"
update_property "$app_id" "sourceCodeUrl" "component_config/sourceCodeUrl.md"
update_property "$app_id" "uiOptions" "component_config/uiOptions.md"

# Update the actions.md file
source "$(dirname "$0")/fn_actions_md_update.sh"
# update_property actions
update_property "$app_id" "actions" "component_config/actions.md"