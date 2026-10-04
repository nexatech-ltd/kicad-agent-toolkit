"""Minimal s-expression helpers for KiCad files.

blocks(text, tag)  (start, end) of top-level blocks, respecting quoted strings
sub(block, tag)    child blocks of one block, same rules
parse(text)        a real parser into nested lists (strings stay strings)
find(node, tag) / val(node, tag)  navigation helpers over the parsed tree

Bracket counting, not regular expressions: a regex over indentation silently returns
plausible but wrong blocks (a non-greedy group drops the last element, a parenthesis
inside quotes breaks the balance).
"""
import re
def blocks(s, tag):
    """(start, end) of top-level '\\n\\t(tag' blocks; quoted strings are respected."""
    res=[]
    for m in re.finditer(r'\n\t\('+re.escape(tag)+r'(?=[\s)])', s):
        i=m.start(); j=i+1; d=0; q=False
        while j<len(s):
            c=s[j]
            if q:
                if c=='\\': j+=2; continue
                if c=='"': q=False
            else:
                if c=='"': q=True
                elif c=='(': d+=1
                elif c==')':
                    d-=1
                    if d==0: break
            j+=1
        res.append((i+1,j+1))
    return res
def sub(blk, tag):
    """Same, but inside a block and at any indentation level."""
    res=[]
    for m in re.finditer(r'\(' + re.escape(tag) + r'(?=[\s)])', blk):
        i=m.start(); j=i; d=0; q=False
        while j<len(blk):
            c=blk[j]
            if q:
                if c=='\\': j+=2; continue
                if c=='"': q=False
            else:
                if c=='"': q=True
                elif c=='(': d+=1
                elif c==')':
                    d-=1
                    if d==0: break
            j+=1
        res.append(blk[i:j+1])
    return res


def parse(text):
    """A real s-expression parser into a tree of lists. Strings stay strings.

    Regex parsing of a netlist "from (net to the next (net" once attached the same
    consumers to every net: block boundaries cannot be found from indentation, and
    kicad-cli indents differently from the editors. Text-based s-expression parsing
    tends to give plausible but wrong results without a single error, so this
    counts brackets instead of lines.
    """
    i, n = 0, len(text)
    stack, cur = [], []
    while i < n:
        c = text[i]
        if c == '(':
            stack.append(cur)
            cur = []
            i += 1
        elif c == ')':
            done = cur
            if not stack:
                raise ValueError('unbalanced closing bracket at %d' % i)
            cur = stack.pop()
            cur.append(done)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                if text[j] == '\\':
                    buf.append(text[j + 1]); j += 2; continue
                if text[j] == '"':
                    break
                buf.append(text[j]); j += 1
            cur.append(''.join(buf))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            cur.append(text[i:j])
            i = j
    if stack:
        raise ValueError('unclosed brackets: %d' % len(stack))
    return cur


def find(tree, tag):
    """All direct sublists whose first element is tag."""
    return [x for x in tree if isinstance(x, list) and x and x[0] == tag]


def val(tree, tag, default=None):
    """Second element of the first (tag v) sublist, the most common case."""
    for x in find(tree, tag):
        if len(x) > 1:
            return x[1]
    return default
