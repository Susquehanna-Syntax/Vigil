"""Test helper: does a real response have the shape its schema documents?

The API schema (sprint M5) describes hand-built responses with serializers
that nothing renders through, so a field renamed in a view would leave the
documentation quietly wrong. ``assert_shape`` compares a response body with
the serializer named in the view's ``@extend_schema``: the same keys at every
level, lists checked item by item. It checks names and nesting, not types.
"""
from rest_framework import serializers


def shape_errors(data, serializer, path="") -> list[str]:
    """Every difference between *data* and *serializer*'s fields."""
    if isinstance(serializer, serializers.ListSerializer):
        if not isinstance(data, list):
            return [f"{path or 'body'}: expected a list"]
        errors = []
        for index, item in enumerate(data):
            errors += shape_errors(item, serializer.child, f"{path}[{index}]")
        return errors
    if isinstance(serializer, type):
        serializer = serializer()
    if not isinstance(serializer, serializers.Serializer):
        return []  # a scalar or free-form field: nothing to compare
    if data is None:
        return [] if serializer.allow_null else [f"{path or 'body'}: null"]
    if not isinstance(data, dict):
        return [f"{path or 'body'}: expected an object"]
    fields = serializer.fields
    errors = [f"{path}.{key}: returned but not documented" for key in data if key not in fields]
    for name, field in fields.items():
        if name not in data:
            if field.required:
                errors.append(f"{path}.{name}: documented but not returned")
            continue
        errors += shape_errors(data[name], field, f"{path}.{name}")
    return errors


def assert_shape(testcase, data, serializer):
    errors = shape_errors(data, serializer)
    testcase.assertEqual(errors, [], "the response and its schema disagree")
