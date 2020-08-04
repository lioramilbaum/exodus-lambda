import json
from datetime import datetime, timezone

import boto3
from cdn_definitions import origin_aliases, rhui_aliases

from .base import LambdaBase


class OriginRequest(LambdaBase):
    def __init__(self, conf_file="lambda_config.json"):
        super().__init__("origin-request", conf_file)
        self._db_client = None

    @property
    def db_client(self):
        if not self._db_client:
            self._db_client = boto3.client("dynamodb", region_name=self.region)

        return self._db_client

    def uri_alias(self, uri, aliases):
        # Resolve every alias between paths within the uri (e.g.
        # allow RHUI paths to be aliased to non-RHUI).
        #
        # Aliases are expected to come from cdn-definitions.

        remaining = aliases

        # We do multiple passes here to ensure that nested aliases
        # are resolved correctly, regardless of the order in which
        # they're provided.
        while remaining:
            processed = []

            for alias in remaining:
                if uri.startswith(alias.src + "/") or uri == alias.src:
                    uri = uri.replace(alias.src, alias.dest, 1)
                    processed.append(alias)

            if not processed:
                # We didn't resolve any alias, then we're done processing.
                break

            # We resolved at least one alias, so we need another round
            # in case others apply now. But take out anything we've already
            # processed, so it is not possible to recurse.
            remaining = [r for r in remaining if r not in processed]

        return uri

    def resolve_aliases(self, uri):
        # aliases relating to origin, e.g. content/origin <=> origin
        uri = self.uri_alias(uri, origin_aliases())

        # aliases relating to rhui; listing files are a special exemption
        # because they must be allowed to differ for rhui vs non-rhui.
        if not uri.endswith("/listing"):
            uri = self.uri_alias(uri, rhui_aliases())

        return uri

    def handler(self, event, context):
        # pylint: disable=unused-argument

        request = event["Records"][0]["cf"]["request"]
        uri = self.resolve_aliases(request["uri"])

        self.logger.info(
            "The request value for origin_request begining is '%s'",
            json.dumps(request, indent=4, sort_keys=True),
        )
        self.logger.info(
            "The uri value for origin_request begining is '%s'", uri
        )

        table = self.conf["table"]["name"]

        self.logger.info("Querying '%s' table for '%s'...", table, uri)

        query_result = self.db_client.query(
            TableName=table,
            Limit=1,
            ScanIndexForward=False,
            KeyConditionExpression="web_uri = :u and from_date <= :d",
            ExpressionAttributeValues={
                ":u": {"S": uri},
                ":d": {
                    "S": str(
                        datetime.now(timezone.utc).isoformat(
                            timespec="milliseconds"
                        )
                    )
                },
            },
        )

        if query_result["Items"]:
            self.logger.info("Item found for '%s'", uri)

            try:
                # Add custom header containing the original request uri
                request["headers"]["exodus-original-uri"] = [
                    {"key": "exodus-original-uri", "value": request["uri"]}
                ]

                # Update request uri to point to S3 object key
                request["uri"] = (
                    "/" + query_result["Items"][0]["object_key"]["S"]
                )
                content_type = query_result["Items"][0]["content_type"]["S"]
                if content_type:
                    request[
                        "querystring"
                    ] = "response-content-type={0}".format(content_type)

                self.logger.info(
                    "The request value for origin_request end is '%s'",
                    json.dumps(request, indent=4, sort_keys=True),
                )
                return request
            except Exception as err:
                self.logger.exception(
                    "Exception occurred while processing %s",
                    json.dumps(query_result["Items"][0]),
                )

                raise err
        else:
            self.logger.info("No item found for '%s'", uri)

            # Report 404 to prevent attempts on S3
            return {"status": "404", "statusDescription": "Not Found"}


# Make handler available at module level
lambda_handler = OriginRequest().handler  # pylint: disable=invalid-name
