#!/usr/bin/env python2.7

"""A script to generate FileCheck statements for 'opt' regression tests.

This script is a utility to update LLVM opt test cases with new
FileCheck patterns. It can either update all of the tests in the file or
a single test function.

Example usage:
$ update_test_checks.py --opt=../bin/opt test/foo.ll

Workflow:
1. Make a compiler patch that requires updating some number of FileCheck lines
   in regression test files.
2. Save the patch and revert it from your local work area.
3. Update the RUN-lines in the affected regression tests to look canonical.
   Example: "; RUN: opt < %s -instcombine -S | FileCheck %s"
4. Refresh the FileCheck lines for either the entire file or select functions by
   running this script.
5. Commit the fresh baseline of checks.
6. Apply your patch from step 1 and rebuild your local binaries.
7. Re-run this script on affected regression tests.
8. Check the diffs to ensure the script has done something reasonable.
9. Submit a patch including the regression test diffs for review.

A common pattern is to have the script insert complete checking of every
instruction. Then, edit it down to only check the relevant instructions.
The script is designed to make adding checks to a test case fast, it is *not*
designed to be authoratitive about what constitutes a good test!
"""

import argparse
import itertools
import os         # Used to advertise this file's name ("autogenerated_note").
import string
import subprocess
import sys
import tempfile
import re

ADVERT = '; NOTE: Assertions have been autogenerated by '

# RegEx: this is where the magic happens.

SCRUB_LEADING_WHITESPACE_RE = re.compile(r'^(\s+)')
SCRUB_WHITESPACE_RE = re.compile(r'(?!^(|  \w))[ \t]+', flags=re.M)
SCRUB_TRAILING_WHITESPACE_RE = re.compile(r'[ \t]+$', flags=re.M)
SCRUB_KILL_COMMENT_RE = re.compile(r'^ *#+ +kill:.*\n')
SCRUB_IR_COMMENT_RE = re.compile(r'\s*;.*')

RUN_LINE_RE = re.compile('^\s*;\s*RUN:\s*(.*)$')
IR_FUNCTION_RE = re.compile('^\s*define\s+(?:internal\s+)?[^@]*@([\w-]+)\s*\(')
OPT_FUNCTION_RE = re.compile(
    r'^\s*define\s+(?:internal\s+)?[^@]*@(?P<func>[\w-]+?)\s*\('
    r'(\s+)?[^)]*[^{]*\{\n(?P<body>.*?)^\}$',
    flags=(re.M | re.S))
CHECK_PREFIX_RE = re.compile('--?check-prefix(?:es)?=(\S+)')
CHECK_RE = re.compile(r'^\s*;\s*([^:]+?)(?:-NEXT|-NOT|-DAG|-LABEL)?:')
# Match things that look at identifiers, but only if they are followed by
# spaces, commas, paren, or end of the string
IR_VALUE_RE = re.compile(r'(\s+)%([\w\.]+?)([,\s\(\)]|\Z)')


# Invoke the tool that is being tested.
def invoke_tool(args, cmd_args, ir):
  with open(ir) as ir_file:
    stdout = subprocess.check_output(args.opt_binary + ' ' + cmd_args,
                                     shell=True, stdin=ir_file)
  # Fix line endings to unix CR style.
  stdout = stdout.replace('\r\n', '\n')
  return stdout


def scrub_body(body, opt_basename):
  # Scrub runs of whitespace out of the assembly, but leave the leading
  # whitespace in place.
  body = SCRUB_WHITESPACE_RE.sub(r' ', body)
  # Expand the tabs used for indentation.
  body = string.expandtabs(body, 2)
  # Strip trailing whitespace.
  body = SCRUB_TRAILING_WHITESPACE_RE.sub(r'', body)
  return body


# Build up a dictionary of all the function bodies.
def build_function_body_dictionary(raw_tool_output, prefixes, func_dict, verbose, opt_basename):
  func_regex = OPT_FUNCTION_RE
  for m in func_regex.finditer(raw_tool_output):
    if not m:
      continue
    func = m.group('func')
    scrubbed_body = scrub_body(m.group('body'), opt_basename)
    if func.startswith('stress'):
      # We only use the last line of the function body for stress tests.
      scrubbed_body = '\n'.join(scrubbed_body.splitlines()[-1:])
    if verbose:
      print >>sys.stderr, 'Processing function: ' + func
      for l in scrubbed_body.splitlines():
        print >>sys.stderr, '  ' + l
    for prefix in prefixes:
      if func in func_dict[prefix] and func_dict[prefix][func] != scrubbed_body:
        if prefix == prefixes[-1]:
          print >>sys.stderr, ('WARNING: Found conflicting asm under the '
                               'same prefix: %r!' % (prefix,))
        else:
          func_dict[prefix][func] = None
          continue

      func_dict[prefix][func] = scrubbed_body


# Create a FileCheck variable name based on an IR name.
def get_value_name(var):
  if var.isdigit():
    var = 'TMP' + var
  var = var.replace('.', '_')
  return var.upper()


# Create a FileCheck variable from regex.
def get_value_definition(var):
  return '[[' + get_value_name(var) + ':%.*]]'


# Use a FileCheck variable.
def get_value_use(var):
  return '[[' + get_value_name(var) + ']]'

# Replace IR value defs and uses with FileCheck variables.
def genericize_check_lines(lines):
  # This gets called for each match that occurs in
  # a line. We transform variables we haven't seen
  # into defs, and variables we have seen into uses.
  def transform_line_vars(match):
    var = match.group(2)
    if var in vars_seen:
      rv = get_value_use(var)
    else:
      vars_seen.add(var)
      rv = get_value_definition(var)
    # re.sub replaces the entire regex match
    # with whatever you return, so we have
    # to make sure to hand it back everything
    # including the commas and spaces.
    return match.group(1) + rv + match.group(3)

  vars_seen = set()
  lines_with_def = []

  for i, line in enumerate(lines):
    # An IR variable named '%.' matches the FileCheck regex string.
    line = line.replace('%.', '%dot')
    # Ignore any comments, since the check lines will too.
    scrubbed_line = SCRUB_IR_COMMENT_RE.sub(r'', line)
    lines[i] =  IR_VALUE_RE.sub(transform_line_vars, scrubbed_line)
  return lines


def add_checks(output_lines, prefix_list, func_dict, func_name, opt_basename):
  # Label format is based on IR string.
  check_label_format = "; %s-LABEL: @%s("

  printed_prefixes = []
  for checkprefixes, _ in prefix_list:
    for checkprefix in checkprefixes:
      if checkprefix in printed_prefixes:
        break
      if not func_dict[checkprefix][func_name]:
        continue
      # Add some space between different check prefixes, but not after the last
      # check line (before the test code).
      #if len(printed_prefixes) != 0:
      #  output_lines.append(';')
      printed_prefixes.append(checkprefix)
      output_lines.append(check_label_format % (checkprefix, func_name))
      func_body = func_dict[checkprefix][func_name].splitlines()

      # For IR output, change all defs to FileCheck variables, so we're immune
      # to variable naming fashions.
      func_body = genericize_check_lines(func_body)

      # This could be selectively enabled with an optional invocation argument.
      # Disabled for now: better to check everything. Be safe rather than sorry.

      # Handle the first line of the function body as a special case because
      # it's often just noise (a useless asm comment or entry label).
      #if func_body[0].startswith("#") or func_body[0].startswith("entry:"):
      #  is_blank_line = True
      #else:
      #  output_lines.append('; %s:       %s' % (checkprefix, func_body[0]))
      #  is_blank_line = False

      is_blank_line = False

      for func_line in func_body:
        if func_line.strip() == '':
          is_blank_line = True
          continue
        # Do not waste time checking IR comments.
        func_line = SCRUB_IR_COMMENT_RE.sub(r'', func_line)

        # Skip blank lines instead of checking them.
        if is_blank_line == True:
          output_lines.append('; %s:       %s' % (checkprefix, func_line))
        else:
          output_lines.append('; %s-NEXT:  %s' % (checkprefix, func_line))
        is_blank_line = False

      # Add space between different check prefixes and also before the first
      # line of code in the test function.
      output_lines.append(';')
      break
  return output_lines


def should_add_line_to_output(input_line, prefix_set):
  # Skip any blank comment lines in the IR.
  if input_line.strip() == ';':
    return False
  # Skip any blank lines in the IR.
  #if input_line.strip() == '':
  #  return False
  # And skip any CHECK lines. We're building our own.
  m = CHECK_RE.match(input_line)
  if m and m.group(1) in prefix_set:
    return False

  return True


def main():
  from argparse import RawTextHelpFormatter
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=RawTextHelpFormatter)
  parser.add_argument('-v', '--verbose', action='store_true',
                      help='Show verbose output')
  parser.add_argument('--opt-binary', default='opt',
                      help='The opt binary used to generate the test case')
  parser.add_argument(
      '--function', help='The function in the test file to update')
  parser.add_argument('tests', nargs='+')
  args = parser.parse_args()

  autogenerated_note = (ADVERT + 'utils/' + os.path.basename(__file__))

  opt_basename = os.path.basename(args.opt_binary)
  if (opt_basename != "opt"):
    print >>sys.stderr, 'ERROR: Unexpected opt name: ' + opt_basename
    sys.exit(1)

  for test in args.tests:
    if args.verbose:
      print >>sys.stderr, 'Scanning for RUN lines in test file: %s' % (test,)
    with open(test) as f:
      input_lines = [l.rstrip() for l in f]

    raw_lines = [m.group(1)
                 for m in [RUN_LINE_RE.match(l) for l in input_lines] if m]
    run_lines = [raw_lines[0]] if len(raw_lines) > 0 else []
    for l in raw_lines[1:]:
      if run_lines[-1].endswith("\\"):
        run_lines[-1] = run_lines[-1].rstrip("\\") + " " + l
      else:
        run_lines.append(l)

    if args.verbose:
      print >>sys.stderr, 'Found %d RUN lines:' % (len(run_lines),)
      for l in run_lines:
        print >>sys.stderr, '  RUN: ' + l

    prefix_list = []
    for l in run_lines:
      (tool_cmd, filecheck_cmd) = tuple([cmd.strip() for cmd in l.split('|', 1)])

      if not tool_cmd.startswith(opt_basename + ' '):
        print >>sys.stderr, 'WARNING: Skipping non-%s RUN line: %s' % (opt_basename, l)
        continue

      if not filecheck_cmd.startswith('FileCheck '):
        print >>sys.stderr, 'WARNING: Skipping non-FileChecked RUN line: ' + l
        continue

      tool_cmd_args = tool_cmd[len(opt_basename):].strip()
      tool_cmd_args = tool_cmd_args.replace('< %s', '').replace('%s', '').strip()

      check_prefixes = [item for m in CHECK_PREFIX_RE.finditer(filecheck_cmd)
                               for item in m.group(1).split(',')]
      if not check_prefixes:
        check_prefixes = ['CHECK']

      # FIXME: We should use multiple check prefixes to common check lines. For
      # now, we just ignore all but the last.
      prefix_list.append((check_prefixes, tool_cmd_args))

    func_dict = {}
    for prefixes, _ in prefix_list:
      for prefix in prefixes:
        func_dict.update({prefix: dict()})
    for prefixes, opt_args in prefix_list:
      if args.verbose:
        print >>sys.stderr, 'Extracted opt cmd: ' + opt_basename + ' ' + opt_args
        print >>sys.stderr, 'Extracted FileCheck prefixes: ' + str(prefixes)

      raw_tool_output = invoke_tool(args, opt_args, test)
      build_function_body_dictionary(raw_tool_output, prefixes, func_dict, args.verbose, opt_basename)

    is_in_function = False
    is_in_function_start = False
    prefix_set = set([prefix for prefixes, _ in prefix_list for prefix in prefixes])
    if args.verbose:
      print >>sys.stderr, 'Rewriting FileCheck prefixes: %s' % (prefix_set,)
    output_lines = []
    output_lines.append(autogenerated_note)

    for input_line in input_lines:
      if is_in_function_start:
        if input_line == '':
          continue
        if input_line.lstrip().startswith(';'):
          m = CHECK_RE.match(input_line)
          if not m or m.group(1) not in prefix_set:
            output_lines.append(input_line)
            continue

        # Print out the various check lines here.
        output_lines = add_checks(output_lines, prefix_list, func_dict, name, opt_basename)
        is_in_function_start = False

      if is_in_function:
        if should_add_line_to_output(input_line, prefix_set) == True:
          # This input line of the function body will go as-is into the output.
          # Except make leading whitespace uniform: 2 spaces.
          input_line = SCRUB_LEADING_WHITESPACE_RE.sub(r'  ', input_line)
          output_lines.append(input_line)
        else:
          continue
        if input_line.strip() == '}':
          is_in_function = False
        continue

      # Discard any previous script advertising.
      if input_line.startswith(ADVERT):
        continue

      # If it's outside a function, it just gets copied to the output.
      output_lines.append(input_line)

      m = IR_FUNCTION_RE.match(input_line)
      if not m:
        continue
      name = m.group(1)
      if args.function is not None and name != args.function:
        # When filtering on a specific function, skip all others.
        continue
      is_in_function = is_in_function_start = True

    if args.verbose:
      print>>sys.stderr, 'Writing %d lines to %s...' % (len(output_lines), test)

    with open(test, 'wb') as f:
      f.writelines([l + '\n' for l in output_lines])


if __name__ == '__main__':
  main()

