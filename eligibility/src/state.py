"""Report once, then remind — never every run.

The sweep runs daily and the upstream check runs daily. Both find the same
things tomorrow that they found today, and a ticket per day per finding is how
a queue becomes something nobody reads. Each (scope, subject) is reported when
first seen and then at most once per remind_days while it persists.

One DynamoDB table, keyed "<scope>#<subject>". Without a table configured,
everything is reported: the failure mode is noise, never silence.
"""

import datetime

import boto3

dynamodb = boto3.client("dynamodb")


def should_emit(table, scope, subject, now, remind_days):
    if not table:
        return True
    key = {"PK": {"S": f"{scope}#{subject}"}}
    item = dynamodb.get_item(TableName=table, Key=key).get("Item")
    last = item and item.get("last_emitted", {}).get("S")
    if last and (now - datetime.datetime.fromisoformat(last)).days < remind_days:
        dynamodb.update_item(TableName=table, Key=key,
                             UpdateExpression="SET last_seen = :n",
                             ExpressionAttributeValues={":n": {"S": now.isoformat()}})
        return False
    dynamodb.update_item(
        TableName=table, Key=key,
        UpdateExpression="SET last_seen = :n, last_emitted = :n, "
                         "first_seen = if_not_exists(first_seen, :n)",
        ExpressionAttributeValues={":n": {"S": now.isoformat()}})
    return True
