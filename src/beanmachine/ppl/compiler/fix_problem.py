# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from abc import ABC, abstractmethod
from typing import Optional, Callable, List, Tuple, Type, Union

import beanmachine.ppl.compiler.bmg_nodes as bn
from beanmachine.ppl.compiler.bm_graph_builder import BMGraphBuilder
from beanmachine.ppl.compiler.error_report import BMGError, ErrorReport
from beanmachine.ppl.compiler.typer_base import TyperBase


# A "node fixer" is a partial function on nodes; it is similar to a "rule". (See rules.py)
# What distinguishes a node fixer from a rule?
#
# * A node fixer is not an instance of a Rule class; it's just a function.
#
# * A node fixer returns:
#   1. None or Inapplicable if the fixer did not know how to fix the problem
#      TODO: Eliminate use of None as a special return value from a node fixer.
#      Node fixers should return Inapplicable, Fatal, or a node.
#   2. The same node as the input, if the node does not actually need fixing.
#   3. A new node, if the fixer did know how to fix the problem
#   4. Fatal, if the node definitely cannot be fixed, so compilation should cease.
#
#   Note the subtle difference between (1) and (2). Suppose we compose a set of n
#   fixers together, as in the first_match combinator below. If the first fixer
#   returns Inapplicable, then we try the second fixer. If the first fixer returns the
#   input, then that fixer is saying that the node is already correct, and we
#   should not try the second fixer.
#
# * A node fixer mutates an existing graph by adding a new node to it; a Rule just
#   returns a success code containing a new value.
#
# * Rules may be combined together with combinators that apply sub-rules to
#   various branches in a large tree, and the result of such a combination is
#   itself a Rule. Node fixers are combined together to form more complex fixers,
#   but they still just operate on individual nodes. The work of applying node fixers
#   over an entire graph is done by a GraphFixer.


class NodeFixerError:
    pass


Inapplicable = NodeFixerError()
Fatal = NodeFixerError()

NodeFixerResult = Union[bn.BMGNode, None, NodeFixerError]
NodeFixer = Callable[[bn.BMGNode], NodeFixerResult]


def node_fixer_first_match(fixers: List[NodeFixer]) -> NodeFixer:
    def first_match(node: bn.BMGNode) -> NodeFixerResult:
        for fixer in fixers:
            result = fixer(node)
            if result is not None and result is not Inapplicable:
                return result
        return Inapplicable

    return first_match


def type_guard(t: Type, fixer: Callable) -> NodeFixer:
    def guarded(node: bn.BMGNode) -> Optional[bn.BMGNode]:
        return fixer(node) if isinstance(node, t) else None

    return guarded


# A GraphFixer is a function that takes no arguments and returns (1) a bool indicating
# whether the graph fixer made any change or not, and (2) an error report. If the
# error report is non-empty then further processing should stop and the error should
# be reported to the user.

GraphFixer = Callable[[], Tuple[bool, ErrorReport]]


def ancestors_first_graph_fixer(  # noqa
    bmg: BMGraphBuilder,
    typer: TyperBase,
    node_fixer: NodeFixer,
    get_error: Optional[Callable[[bn.BMGNode, int], Optional[BMGError]]] = None,
) -> GraphFixer:
    # Applies the node fixer to each node in the graph builder that is an ancestor,
    # of any sample, query, or observation, starting with ancestors and working
    # towards decendants. Fixes are done one *edge* at a time. That is, when
    # we enumerate a node, we check all its input edges to see if the input node
    # needs to be fixed, and if so, then we update that edge to point from
    # the fixed node to its new output.
    #
    # We enumerate each output node once, but because we then examine each of its
    # input edges, we will possibly encounter the same input node more than once.
    #
    # Rather than rewriting it again, we memoize the result and reuse it.
    # If a fixer indicates a fatally unfixable node then we attempt to report an error
    # describing the problem with the edge. However, we will continue to run fixers
    # on other nodes, hoping that we might report more errors.
    #
    # A typer associates type information with each node in the graph. We have some
    # problems though:
    #
    # * We frequently need to accurately know the type of a node when checking to
    #   see if it needs fixing.
    # * Computing the type of a node requires computing the types of all of its
    #   *ancestor* nodes, which can be quite expensive.
    # * If a mutation changes an input of a node, that node's type might change,
    #   which could then change the types of all of its *descendant* nodes.
    #
    # We solve this performance problem by (1) computing types of nodes on demand
    # and caching the result, (2) being smart about recomputing the type of a node
    # and its descendants when the graph is mutated.  We therefore tell the typer
    # that it needs to re-type a node and its descendants only when a node changes.
    #
    # CONSIDER: Could we use a simpler algorithm here?  That is: for each node,
    # try to fix the node. If successful, remove all the output edges of the old
    # node and add output edges to the new node.  The problem with this approach
    # is that we might end up reporting an error on an edge that is NOT in the
    # subgraph of ancestors of samples, queries and observations, which would be
    # a bad user experience.
    def ancestors_first() -> Tuple[bool, ErrorReport]:
        errors = ErrorReport()
        replacements = {}
        reported = set()
        nodes = bmg.all_ancestor_nodes()
        made_progress = False
        for node in nodes:
            node_was_updated = False
            for i in range(len(node.inputs)):
                c = node.inputs[i]
                # Have we already reported an error on this node? Skip it.
                if c in reported:
                    continue
                # Have we already replaced this input with something?
                # If so, no need to compute the replacement again.
                if c in replacements:
                    if node.inputs[i] is not replacements[c]:
                        node.inputs[i] = replacements[c]
                        node_was_updated = True
                    continue

                replacement = node_fixer(c)

                if isinstance(replacement, bn.BMGNode):
                    replacements[c] = replacement
                    if node.inputs[i] is not replacement:
                        node.inputs[i] = replacement
                        node_was_updated = True
                        made_progress = True
                elif replacement is Fatal:
                    reported.add(c)
                    if get_error is not None:
                        error = get_error(node, i)
                        if error is not None:
                            errors.add_error(error)

            if node_was_updated:
                typer.update_type(node)
        return made_progress, errors

    return ancestors_first


# TODO: Create a match-first combinator on GraphFixers.
# TODO: Create a fixpoint combinator on GraphFixers.


# TODO: Eventually this base class will be refactored away and
# only GraphFixer / NodeFixer will remain.


class ProblemFixerBase(ABC):
    _bmg: BMGraphBuilder
    _typer: TyperBase
    errors: ErrorReport

    def __init__(self, bmg: BMGraphBuilder, typer: TyperBase) -> None:
        self._bmg = bmg
        self._typer = typer
        self.errors = ErrorReport()

    @abstractmethod
    def _needs_fixing(self, n: bn.BMGNode) -> bool:
        pass

    def _get_replacement(self, n: bn.BMGNode) -> Optional[bn.BMGNode]:
        pass

    def _get_error(self, n: bn.BMGNode, index: int) -> Optional[BMGError]:
        # n.inputs[i] needed fixing but was unfixable. If that needs to
        # produce an error, do by overriding this method
        return None

    def fix_problems(self) -> None:
        replacements = {}
        reported = set()
        nodes = self._bmg.all_ancestor_nodes()
        for node in nodes:
            node_was_updated = False
            for i in range(len(node.inputs)):
                c = node.inputs[i]
                # Have we already replaced this input with something?
                # If so, no need to compute the replacement again.
                if c in replacements:
                    if node.inputs[i] is not replacements[c]:
                        node.inputs[i] = replacements[c]
                        node_was_updated = True
                    continue
                # Does the input need fixing at all?
                if not self._needs_fixing(c):
                    continue
                # The input needs fixing. Get the replacement.
                replacement = self._get_replacement(c)
                if replacement is not None:
                    replacements[c] = replacement
                    if node.inputs[i] is not replacement:
                        node.inputs[i] = replacement
                        node_was_updated = True
                    continue
                # It needed fixing but we did not succeed. Have we already
                # reported this error?  If so, no need to compute the error.
                if c in reported:
                    continue
                # Mark the node as having been error-reported, and emit
                # an error into the error report.
                reported.add(c)
                error = self._get_error(node, i)
                if error is not None:
                    self.errors.add_error(error)
            if node_was_updated:
                self._typer.update_type(node)
