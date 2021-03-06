from marshmallow import validates_schema, ValidationError, fields, post_dump, post_load, UnmarshalResult
from marshmallow.validate import OneOf
from marshmallow_sqlalchemy import ModelSchema, field_for

import walkoff.executiondb as executiondb
from walkoff.helpers import InvalidExecutionElement
from .action import Action
from .argument import Argument
from .branch import Branch
from .condition import Condition
from .conditionalexpression import ConditionalExpression, valid_operators
from .executionelement import ExecutionElement
from .playbook import Playbook
from .position import Position
from .transform import Transform
from .workflow import Workflow


class ExecutionBaseSchema(ModelSchema):
    """Base schema for the execution database.

    This base class adds functionality to strip null fields from serialized objects and attaches the
    execution_db.session on load
    """
    __skipvalues = (None, [], [{}])

    @post_dump
    def _do_post_dump(self, data):
        return self.remove_skip_values(data)

    def remove_skip_values(self, data):
        """Removes fields with empty values from data

        Args:
            data (dict): The data passed in

        Returns:
            (dict): The data with forbidden fields removed
        """
        return {
            key: value for key, value in data.items()
            if value not in self.__skipvalues
        }

    def load(self, data, session=None, instance=None, *args, **kwargs):
        session = executiondb.execution_db.session
        # Maybe automatically find and use instance if 'id' (or key) is passed
        return super(ExecutionBaseSchema, self).load(data, session=session, instance=instance, *args, **kwargs)


class ExecutionElementBaseSchema(ExecutionBaseSchema):
    """The base schema for execution elements

    This class validates the deserialized execution elements
    """

    def load(self, data, session=None, instance=None, *args, **kwargs):
        # Can't use post_load because we can't override marshmallow_sqlalchemy's post_load hook
        try:
            objs = super(ExecutionElementBaseSchema, self).load(data, session=session, instance=instance, *args,
                                                                **kwargs)
        except InvalidExecutionElement as e:
            objs = UnmarshalResult({}, e.errors)
        if not objs.errors:
            errors = {}
            all_objs = objs.data
            if not isinstance(objs.data, list):
                all_objs = [all_objs]
            for obj in (obj for obj in all_objs if isinstance(obj, ExecutionElement)):
                try:
                    obj.validate()
                except InvalidExecutionElement as e:
                    errors[e.name] = e.errors
            if errors:
                objs.errors.update(errors)
        return objs


class ArgumentSchema(ExecutionBaseSchema):
    """The schema for arguments.

    This class handles constructing the argument specially so that either a reference or a value is always non-null,
    but never both.
    """
    name = field_for(Argument, 'name', required=True)
    value = fields.Raw()
    selection = fields.List(fields.Raw())  # There should be some validation on this maybe

    class Meta:
        model = Argument

    @validates_schema
    def validate_argument(self, data):
        has_value = 'value' in data
        has_reference = 'reference' in data and bool(data['reference'])
        if (not has_value and not has_reference) or (has_value and has_reference):
            raise ValidationError('Arguments must have either a value or a reference.', ['value'])

    @post_load
    def make_instance(self, data):
        instance = self.instance or self.get_instance(data)
        if instance is not None:
            value = data.pop('value', None)
            reference = data.pop('reference', None)
            instance.update_value_reference(value, reference)
            for key, value in data.items():
                setattr(instance, key, value)
            return instance
        return self.opts.model(**data)


class ActionableSchema(ExecutionElementBaseSchema):
    """Base schema for execution elements with actions
    """
    app_name = fields.Str(required=True)
    action_name = fields.Str(required=True)
    arguments = fields.Nested(ArgumentSchema, many=True)


class TransformSchema(ActionableSchema):
    """Schema for transforms
    """

    class Meta:
        model = Transform


class ConditionSchema(ActionableSchema):
    """Schema for conditions
    """

    transforms = fields.Nested(TransformSchema, many=True)
    is_negated = field_for(Condition, 'is_negated', default=False)

    class Meta:
        model = Condition


class ConditionalExpressionSchema(ExecutionElementBaseSchema):
    """Schema for conditional expressions
    """
    conditions = fields.Nested(ConditionSchema, many=True)
    child_expressions = fields.Nested('self', many=True)
    operator = field_for(ConditionalExpression, 'operator', default='and', validates=OneOf(*valid_operators),
                         missing='and')
    is_negated = field_for(ConditionalExpression, 'is_negated', default=False)

    class Meta:
        model = ConditionalExpression
        excludes = ('parent',)


class BranchSchema(ExecutionElementBaseSchema):
    """Schema for branches
    """
    source_id = field_for(Branch, 'source_id', required=True)
    destination_id = field_for(Branch, 'destination_id', required=True)
    condition = fields.Nested(ConditionalExpressionSchema())
    priority = field_for(Branch, 'priority', default=999)
    status = field_for(Branch, 'status', default='Success')

    class Meta:
        model = Branch


class PositionSchema(ExecutionBaseSchema):
    """Schema for positions
    """
    x = field_for(Position, 'x', required=True)
    y = field_for(Position, 'y', required=True)

    class Meta:
        model = Position


class ActionSchema(ActionableSchema):
    """Schema for actions
    """
    trigger = fields.Nested(ConditionalExpressionSchema())
    position = fields.Nested(PositionSchema())

    class Meta:
        model = Action


class WorkflowSchema(ExecutionElementBaseSchema):
    """Schema for workflows
    """
    name = field_for(Workflow, 'name', required=True)
    actions = fields.Nested(ActionSchema, many=True)
    branches = fields.Nested(BranchSchema, many=True)

    class Meta:
        model = Workflow
        exclude = ('playbook',)


class PlaybookSchema(ExecutionElementBaseSchema):
    """Schema for playbooks
    """
    name = field_for(Playbook, 'name', required=True)
    workflows = fields.Nested(WorkflowSchema, many=True)

    class Meta:
        model = Playbook


# This could be done better with a metaclass which registers subclasses
_schema_lookup = {
    Playbook: PlaybookSchema,
    Workflow: WorkflowSchema,
    Action: ActionSchema,
    Branch: BranchSchema,
    ConditionalExpression: ConditionalExpressionSchema,
    Condition: ConditionSchema,
    Transform: TransformSchema,
    Position: PositionSchema,
    Argument: ArgumentSchema}


def dump_element(element):
    """Dumps an execution element

    Args:
        element (ExecutionElement): The element to dump

    Returns:
        dict: The serialized element
    """
    return _schema_lookup[element.__class__]().dump(element).data
