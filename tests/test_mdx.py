import unittest

from repo_kbs_sync.mdx import preprocess_mdx_text


class MdxTests(unittest.TestCase):
    def test_removes_mdx_syntax_and_keeps_component_children(self):
        source = """import Tabs from '@theme/Tabs';

# Guide

<Tabs>
  <TabItem value="one">
    Read **this**.
  </TabItem>
</Tabs>

{showExtra && <Note>not static</Note>}
"""
        result = preprocess_mdx_text(source)
        self.assertNotIn("import Tabs", result)
        self.assertNotIn("<Tabs", result)
        self.assertIn("Read **this**.", result)
        self.assertNotIn("showExtra", result)

    def test_protects_frontmatter_and_code(self):
        source = """---
title: '<Widget>'
export: true
---

```mdx
<Widget />
{literal}
```

`<Widget />`
"""
        result = preprocess_mdx_text(source)
        self.assertIn("title: '<Widget>'", result)
        self.assertIn("export: true", result)
        self.assertIn("<Widget />", result)
        self.assertIn("{literal}", result)

    def test_removes_multiline_export(self):
        source = """export const metadata = {
  title: 'not a document paragraph',
};

# Actual title
"""
        result = preprocess_mdx_text(source)
        self.assertNotIn("metadata", result)
        self.assertNotIn("not a document paragraph", result)
        self.assertIn("# Actual title", result)

    def test_removes_plain_javascript_expression(self):
        result = preprocess_mdx_text("Value: {user.name + '!'}.\n")
        self.assertEqual(result, "Value: .\n")
