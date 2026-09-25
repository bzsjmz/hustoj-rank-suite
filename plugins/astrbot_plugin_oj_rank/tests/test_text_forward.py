"""Verify long text is folded into one forwarded message."""
import ast
import rank_data as data
from pathlib import Path
from types import SimpleNamespace
import unittest


class TextForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text())
        plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "OjRankPlugin")
        method = next(node for node in plugin.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_text_result")
        method.decorator_list = []
        namespace = {
            "data": data,
            "FORWARD_TEXT_LENGTH": 100,
            "Node": lambda **kwargs: SimpleNamespace(**kwargs),
            "Plain": lambda value: SimpleNamespace(text=value),
            "AstrMessageEvent": object,
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), "helper", "exec"), namespace)
        cls.text_result = staticmethod(namespace["_text_result"])
        cls.plugin = plugin

    def setUp(self):
        self.event = SimpleNamespace(
            get_self_id=lambda: "123",
            plain_result=lambda content: ("plain", content),
            chain_result=lambda components: ("chain", components),
        )

    def test_short_message_stays_plain(self):
        self.assertEqual(("plain", "a" * 100), self.text_result(self.event, "a" * 100))

    def test_long_message_uses_one_forward_with_full_content(self):
        content = "榜单\n" + "名次 姓名\n" * 80
        kind, components = self.text_result(self.event, content)
        self.assertEqual("chain", kind)
        self.assertEqual(1, len(components))
        node = components[0]
        self.assertEqual("123", node.uin)
        self.assertEqual(1, len(node.content))
        self.assertEqual(content, node.content[0].text)

    def test_boundary_and_help_text_are_not_split(self):
        sample = "甲" * 101
        kind, components = self.text_result(self.event, sample)
        self.assertEqual("chain", kind)
        self.assertEqual(sample, components[0].content[0].text)

        tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text())
        help_text = next(ast.literal_eval(node.value) for node in tree.body
                         if isinstance(node, ast.Assign)
                         and any(isinstance(target, ast.Name) and target.id == "HELP_TEXT"
                                 for target in node.targets))
        kind, components = self.text_result(self.event, help_text)
        self.assertEqual("chain", kind)
        self.assertEqual(help_text, components[0].content[0].text)

    def test_only_helper_sends_plain_text_directly(self):
        direct = [node.lineno for node in ast.walk(self.plugin)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "plain_result"]
        self.assertEqual(1, len(direct))
