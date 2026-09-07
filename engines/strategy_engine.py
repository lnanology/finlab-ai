import ast
import json
import logging
import operator

logger = logging.getLogger(__name__)


class ConditionEvaluationError(Exception):
    """Raised when a strategy rule's condition string can't be safely
    parsed/evaluated -- e.g. a typo in a strategies/*.json file, or
    (should this module's trust model ever change) a condition string
    containing disallowed syntax. StrategyEngine._evaluate_condition
    catches this and treats it as "condition not met" (contributes 0 to
    the score), not a crash of the whole score calculation."""


_ALLOWED_COMPARE_OPS = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


class _SafeConditionEvaluator(ast.NodeVisitor):
    """Restricted evaluator for strategy-rule condition strings like
    "data['volume_ratio'] > 2" or "data['price'] > 100 and data['rsi'] < 70".

    2026-09-07 (security review): replaces a bare eval(condition,
    {"data": data}) call. These condition strings have always come from
    this repo's own bundled strategies/*.json files -- no endpoint
    writes to strategies/*.json, so the old eval() was never directly
    reachable with attacker-controlled input. Still, eval() on any
    string is a foot-gun that's cheap to remove: this walks a real
    ast.parse() tree and only permits the handful of node types an
    "and"/"or"-joined chain of dict-lookup comparisons actually needs
    (Compare, BoolOp, UnaryOp for "not", Subscript on the single `data`
    name, Name, Constant) -- anything else (function calls, attribute
    access, imports, comprehensions, walrus assignment, ...) raises
    ConditionEvaluationError instead of silently running."""

    def __init__(self, data: dict):
        self.data = data

    def visit(self, node):
        method = "visit_" + node.__class__.__name__
        visitor = getattr(self, method, None)
        if visitor is None:
            raise ConditionEvaluationError(f"Disallowed syntax in condition: {node.__class__.__name__}")
        return visitor(node)

    def visit_Expression(self, node):
        return self.visit(node.body)

    def visit_BoolOp(self, node):
        values = [self.visit(v) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise ConditionEvaluationError("Disallowed boolean operator")

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return not self.visit(node.operand)
        raise ConditionEvaluationError("Disallowed unary operator")

    def visit_Compare(self, node):
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            op_fn = _ALLOWED_COMPARE_OPS.get(type(op))
            if op_fn is None:
                raise ConditionEvaluationError(f"Disallowed comparison operator: {type(op).__name__}")
            right = self.visit(comparator)
            if not op_fn(left, right):
                return False
            left = right
        return True

    def visit_Subscript(self, node):
        value = self.visit(node.value)
        key_node = node.slice
        key = self.visit(key_node) if isinstance(key_node, ast.AST) else key_node
        try:
            return value[key]
        except (KeyError, TypeError, IndexError) as e:
            raise ConditionEvaluationError(f"Bad subscript lookup: {e}")

    def visit_Name(self, node):
        if node.id == "data":
            return self.data
        raise ConditionEvaluationError(f"Disallowed name: {node.id!r} (only 'data' is permitted)")

    def visit_Constant(self, node):
        return node.value


def safe_eval_condition(condition: str, data: dict) -> bool:
    """Parse and evaluate a strategy-rule condition string using only the
    restricted grammar _SafeConditionEvaluator permits. Raises
    ConditionEvaluationError on any disallowed syntax or lookup failure
    -- see that class's docstring for the full rationale."""
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError as e:
        raise ConditionEvaluationError(f"Invalid condition syntax: {e}")
    return bool(_SafeConditionEvaluator(data).visit(tree))


class StrategyEngine:
    """
    Strategy Engine™ 用於加載策略規則、計算分數和生成信號。
    """

    def __init__(self, strategy_filepath):
        """
        初始化引擎並加載策略規則。

        參數：
        strategy_filepath (str): JSON 策略文件的相對路徑。
        """
        self.strategy_filepath = strategy_filepath
        self.strategy_rules = self._load_strategy()

    def _load_strategy(self):
        """
        從 JSON 文件中加載策略規則。

        返回：
        dict: 策略規則。
        """
        with open(self.strategy_filepath, "r") as f:
            return json.load(f)

    def calculate_score(self, data):
        """
        根據策略規則計算分數（0-100）。

        參數：
        data (dict): 輸入數據，包含策略規則所需的所有參數。

        返回：
        int: 分數（0-100）。
        """
        score = 0
        for rule in self.strategy_rules.get("rules", []):
            condition = rule.get("condition")
            weight = rule.get("weight")
            if self._evaluate_condition(condition, data):
                score += weight
        return min(max(score, 0), 100)  # 確保分數在 0-100 之間

    def generate_signal(self, score):
        """
        根據分數生成信號（Bullish/Neutral/Bearish）。

        參數：
        score (int): 計算出的分數（0-100）。

        返回：
        str: 信號（Bullish/Neutral/Bearish）。
        """
        if score >= 70:
            return "Bullish"
        elif 30 <= score < 70:
            return "Neutral"
        else:
            return "Bearish"

    def _evaluate_condition(self, condition, data):
        """
        評估條件是否成立。

        參數：
        condition (str): 條件表達式，例如 "data['price'] > 100"。
        data (dict): 輸入數據。

        返回：
        bool: 條件是否成立。
        """
        try:
            return safe_eval_condition(condition, data)
        except ConditionEvaluationError as e:
            # Treated as "condition not met" (contributes 0 to the score)
            # rather than crashing the whole calculate_score() call --
            # see safe_eval_condition()'s docstring above for why this
            # replaced a bare eval() call.
            logger.warning("StrategyEngine: skipping unevaluable condition %r: %s", condition, e)
            return False


# 示例用法
if __name__ == "__main__":
    # 示例策略 JSON 文件
    strategy_filepath = "strategies/AJ_Strategy_V1.json"

    # 初始化引擎
    engine = StrategyEngine(strategy_filepath)

    # 示例輸入數據
    data = {
        "price": 120,
        "volume": 1000,
        "sentiment": "Bullish",
    }

    # 計算分數
    score = engine.calculate_score(data)
    print(f"Score: {score}")

    # 生成信號
    signal = engine.generate_signal(score)
    print(f"Signal: {signal}")
