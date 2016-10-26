#!/usr/bin/env python

from mmap import mmap, ACCESS_READ
from contextlib import closing
from blessings import Terminal
from argparse import ArgumentParser
import re, operator

FUN_RE = re.compile('L(.+?);->(.+)$')
FNP_RE = re.compile(r'^(.+?)\((.*)\)(.+)$')
INS_RE = re.compile(br'^\s+(\w\S+) (?:(.*?), )?(\S+)$', re.MULTILINE)
END_RE = re.compile(br'\.end method', re.MULTILINE)
PARAM_RE = re.compile(r'\[*(?:L.+?;|[^L])')

T = Terminal()

def main():
    parser = ArgumentParser(description='Performs primitive Dalvik symbolic execution')
    parser.add_argument('entry_point', metavar='Lhu/silentsignal/class;->method(I)I',
            help='entry point for symbolic execution')
    parser.add_argument('--tracer', dest='tracer_class', metavar='TracerClass',
            default='Tracer', help='tracer class to instantiate')
    parser.add_argument('--trace-locals', dest='trace_locals', action='store_true',
            default=False, help='trace changes in local variables')
    parser.add_argument('--show-instructions', dest='show_insn', action='store_true',
            default=False, help='show instructions with byte offsets')
    args = parser.parse_args()
    globals()[args.tracer_class](args).trace_fun(args.entry_point)

class StringValue(object):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        v = self.value
        lv = len(v)
        if lv > 30:
            v = v[:30] + '...[{0} chars]'.format(lv)
        return '"{0}"'.format(v)

class SimpleResult(object):
    def __init__(self, value):
        self.value = value

MATH_OPS = {
        'add': ('+', operator.__add__),
        'and': ('&', operator.__and__),
        'div': ('/', operator.__div__),
        'mul': ('*', operator.__mul__),
        'shl': ('<<', operator.__lshift__),
        'shr': ('>>', operator.__rshift__),
        'sub': ('-', operator.__sub__),
        }

CONDITIONS = {
        'nez': ('!= null', False),
        'ge':  ('>=', True),
        'gt':  ('>',  True),
        'le':  ('<=', True),
        'lt':  ('<',  True),
        }

class Tracer(object):
    level = 0
    variables = 0

    def __init__(self, args):
        self.args = args

    def trace(self, text):
        print self.level * '  ' + text

    def trace_fun(self, function, params=None, instance=None):
        cls, meth = FUN_RE.match(function).groups()
        name, param_list, retval_type = FNP_RE.match(meth).groups()
        param_types = PARAM_RE.findall(param_list)
        if params is None:
            params = []
            for p in param_types:
                params.append(chr(ord('a') + self.variables))
                self.variables += 1
        self.trace(T.yellow(function) + ' // (' + repr(instance) + ') ' + repr(params))
        if cls.startswith('java'):
            if function == 'Ljava/lang/String;->length()I':
                if isinstance(instance, StringValue):
                    return SimpleResult(len(instance.value))
                return SimpleResult('strlen({0})'.format(instance))
            elif function == 'Ljava/lang/String;->charAt(I)C':
                if isinstance(instance, StringValue) and isinstance(params[0], int):
                    return SimpleResult(ord(instance.value[params[0]]))
                return SimpleResult('{0}[{1}]'.format(instance, params[0]))
            return # TODO
        local_variables = {}
        if instance is not None:
            params.insert(0, instance)
        with open('smali/' + cls + '.smali') as f:
            with closing(mmap(f.fileno(), 0, access=ACCESS_READ)) as smali:
                m = re.search(r'^\.method .* ' + re.escape(meth) + '$', smali, re.MULTILINE)
                pos = m.end()
                m = END_RE.search(smali, pos)
                return self.trace_body(smali, pos, m.start(), params, local_variables)

    def trace_body(self, smali, start, end, params, local_variables, orig_start=None):
        def decode_op(op):
            if op.startswith('p'):
                return params[int(op[1:])]
            elif op.startswith('v'):
                return local_variables[op]
            raise ValueError('Invalid operand ' + repr(op))

        if orig_start is None:
            orig_start = start

        last_lv = {}
        for m in INS_RE.finditer(smali, start, end):
            isn, p1, p2 = m.groups()
            if self.args.show_insn:
                self.trace(str(m.start()) + ' @ ' + repr(m.groups())[:80])
            if isn.startswith('invoke-'):
                cp = []
                if len(p1) > 2:
                    for iparam in p1[1:-1].split(', '):
                        cp.append(decode_op(iparam))
                instance = None if isn.endswith('static') else cp.pop(0)
                self.level += 1
                result = self.trace_fun(p2, cp, instance)
                self.level -= 1
                if isinstance(result, SimpleResult):
                    last_result = result.value
                else:
                    last_result = 'result@{0}'.format(m.start())
            elif isn == 'return':
                value = local_variables[p2]
                self.trace(T.red('return {0}'.format(value)))
                return SimpleResult(value)
            elif isn == 'return-object':
                value = local_variables[p2]
                if value == 0:
                    value = 'null'
                self.trace(T.red('return {0}'.format(value)))
                return SimpleResult(value)
            elif isn.startswith('move-result'):
                local_variables[p2] = last_result
            elif isn.startswith('const/') or isn == 'const':
                local_variables[p1] = int(p2, 16)
            elif isn == 'const-string':
                local_variables[p1] = StringValue(p2[1:-1])
            elif isn.endswith('-int') and isn.split('-', 1)[0] in MATH_OPS:
                op_re, op_fn = MATH_OPS[isn.split('-', 1)[0]]
                target, source = p1.split(', ', 1)
                ds = decode_op(source)
                dd = decode_op(p2)
                if isinstance(ds, int) and isinstance(dd, int):
                    local_variables[target] = op_fn(ds, dd)
                else:
                    local_variables[target] = '({s} {o} {d})'.format(s=ds, o=op_re, d=decode_op(p2))
            elif '-int/2addr' in isn and isn.split('-', 1)[0] in MATH_OPS:
                op_re, op_fn = MATH_OPS[isn.split('-', 1)[0]]
                dt = decode_op(p1)
                ds = decode_op(p2)
                if isinstance(dt, int) and isinstance(ds, int):
                    local_variables[p1] = op_fn(dt, ds)
                else:
                    local_variables[p1] = '({s} {o} {d})'.format(s=ds, o=op_re, d=dt)
            elif '-int/lit' in isn and isn.split('-', 1)[0] in MATH_OPS:
                op_re, op_fn = MATH_OPS[isn.split('-', 1)[0]]
                target, source = p1.split(', ', 1)
                ds = decode_op(source)
                dd = int(p2, 16)
                if isinstance(ds, int):
                    local_variables[target] = op_fn(ds, dd)
                else:
                    local_variables[target] = '({s} {o} {d})'.format(s=ds, o=op_re, d=dd)
            elif isn == 'new-array':
                target, size = p1.split(', ', 1)
                local_variables[target] = 'new {t}[{s}]'.format(t=p2[-1], s=decode_op(size))
            elif isn == 'int-to-byte':
                dd = decode_op(p2)
                if isinstance(dd, int):
                    local_variables[p1] = dd & 0xFF
                else:
                    local_variables[p1] = '((byte){0})'.format(dd)
            elif isn == 'new-instance':
                local_variables[p1] = 'new ' + p2
            elif isn == 'sget-object':
                local_variables[p1] = 'get ' + p2
            elif isn.startswith('if-'):
                op_re, has_params = CONDITIONS[isn[3:]]
                if has_params:
                    o1, o2 = p1.split(', ', 1)
                    self.trace(T.blue('if ({0} {1} {2}) goto {3}'.format(decode_op(o1), op_re, decode_op(o2), p2)))
                else:
                    self.trace(T.blue('if ({0} {1}) goto {2}'.format(decode_op(p1), op_re, p2)))
                self.trace(T.cyan('--- If jump is taken: ---'))
                jump = smali.find('\n    ' + p2, orig_start, end)
                self.level += 1
                self.trace_body(smali, jump, end, params, local_variables.copy(), orig_start=orig_start)
                self.level -= 1
                self.trace(T.cyan('--- If jump is not taken: ---'))
            elif isn == 'aput-byte':
                value, array = p1.split(', ', 1)
                self.trace(T.blue('{array}[{index}] = {value}'.format(array=array, value=value, index=p2)))
            elif isn == 'goto':
                jump = smali.find('\n    ' + p2, orig_start, end)
                return self.trace_body(smali, jump, end, params, local_variables.copy(), orig_start=orig_start)
            else:
                raise NotImplementedError
            if self.args.trace_locals:
                if local_variables and last_lv != local_variables:
                    self.trace(T.green(repr(local_variables)))
                    last_lv = local_variables.copy()
        # TODO m.end()


class PrintTracer(Tracer):
    def trace(self, text):
        pass

    def trace_body(self, smali, start, end, params, local_variables):
        print smali[start:end].strip('\n')


if __name__ == '__main__':
    main()
