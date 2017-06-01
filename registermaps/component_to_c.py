#!/usr/bin/env python

"""
Generate C header files from HTI XML register description documents.

This program parses two different types of XML description file.
First, a file documenting a single component, and all of the
registers that it may contain.  Second, a file documenting an
overall memory map, which instantiates and calls out various
components.

The structure of the program is to attach new outputter methods
to the classes defined by hti_reg_xml, and then call those methods
on the objects they're bound to.  This seems a bit overly
complicated, but is the cleanest way to deal with the existance
of Arrays, which may nest arbitrary arguments.
"""

import textwrap
import datetime
import argparse
import traceback
import codecs
import os
import sys
import itertools
from StringIO import StringIO

import space
from hti_reg_xml import *

class OutputterError(Exception):
    """
    An error because the outputter doesn't know what to do.
    First argument is a string description, second is the
    HtiElement where the error occurred.
    """
    pass

storage_class_definitions = """
#ifndef __I
#define __I     volatile const          /*!< defines 'read only' permissions      */
#endif

#ifndef __O
#define __O     volatile                /*!< defines 'write only' permissions     */
#endif

#ifndef __IO
#define __IO    volatile                /*!< defines 'read / write' permissions   */
#endif

#include <stdint.h>
"""

########################################################################
# Much to do to enable good looking comments in the output.
########################################################################

class CommentFormatter:
    """Accept an array of lines, formats them as a comment between full /* */ bars"""
    tw = textwrap.TextWrapper(
        initial_indent = '',
        subsequent_indent = ''
    )

    def __init__(self, width=79, bars=True, indent = 0):
        self.bars = bars
        self.width = width
        self.indent = indent

    def format(self, text):
        """
        Format a list of lines between horizontal bars.
        """

        indent = ' ' * self.indent

        if (self.bars):
            topbar              = indent + '/*' + ('*' * (self.width - self.indent - 2))
            initial_indent      = indent + ' * '
            subsequent_indent   = indent + ' * '
            bottombar           = indent + ' ' + ('*' * (self.width - self.indent - 3)) + '*/'
        else:
            topbar              = ''
            initial_indent      = indent + '/* '
            subsequent_indent   = indent + ' * '
            bottombar           = indent + ' */'

        self.tw.width = self.width - len(initial_indent)
        
        comment = []
        for line in text.strip().splitlines():
            wrapped_lines = self.tw.wrap(line)
            if wrapped_lines:
                comment.extend(wrapped_lines)
            else:
                comment.append("")
        
        block = [topbar, (initial_indent + comment[0])]
        block.extend([subsequent_indent + l for l in comment[1:]])
        block.append(bottombar)
        
        return "\n".join(block)

maincomment = CommentFormatter(indent = 0, bars = True)
subcomment = CommentFormatter(indent = 4, bars = False)

########################################################################
# General purpose output formatting stuff
########################################################################

def register_format(reg):
    """Return the storage specification for a given register."""
    if reg.get('readOnly', False):
        storage = "__I "
    elif reg.get('writeOnly', False):
        storage = "__O "
    else:
        storage = "__IO"

    if (reg.get('format', '').lower() == 'signed'):
        return storage + ' int32_t'
    else:
        return storage + ' uint32_t'

def make_header_filename(output_dir, sourcefile):
    """The name of a header file generated by a given source file."""
    basename = os.path.basename(sourcefile)
    (root, ext) = os.path.splitext(basename)
    basename = root + ".h"
    
    if output_dir:
        return os.path.join(output_dir, basename)
    else:
        return basename

def define(name, val):
    return '#define {0:39s} ({1})'.format(name, val)

########################################################################
# Routines for outputting Components
########################################################################

#
# We need to output structs from (Arrays of) Registers.
#

def generate_struct_innards(space, indent, output):
    """Generates the interior bits of struct from a space full of Registers."""
    for ptr in space:
        if ptr:
            # Real element
            ptr.obj.generate_struct(indent, output)
        else:
            # Gap
            for i in range(ptr.pos, ptr.pos + ptr.size):
                print >>output, indent + '__I  uint32_t rsvd{0};'.format(i)

def generate_array_struct(self, indent, output):
    """Create a struct inside the typedef struct."""
    if (len(self.space) > 1):        
        print >>output, indent + 'struct {'
        generate_struct_innards(self.space, indent + '    ', output)
        print >>output, indent + '}} {0}[{1}];'.format(
            self['name'],
            self['count']
        )
    else:
        s = StringIO()
        for ptr in itertools.ifilter(bool, self.space):
            ptr.obj.generate_struct(indent, s)
            
        print >>output, s.getvalue().rstrip(";\n") + '[{0}]'.format(self['count'])
    
def generate_register_struct(self, indent, output):
    """Place a register into the struct."""
    if (self['size'] != 1):
        raise OutputterError(
            "Don't know how to deal with register sizes other than 1.",
            self)
        
    desc = self.textDescription();
    if desc:
        print >>output, subcomment.format(desc)
            
    print >>output, indent + '{fmt:15s} {fn};'.format(
                        fmt = register_format(self),
                        fn = self['name'])

RegisterArray.generate_struct = generate_array_struct
Register.generate_struct = generate_register_struct

#
# We need to output bitfields from (Arrays of) Fields
#

def generate_bitfields(space, compname, output):
    """Generate the bitfields for the space, with as much recursion as necessary."""
    for ptr in itertools.ifilter(bool, space):
        if ptr.obj.space:
            ptr.obj.generate_bitfields(compname, output)
   
def generate_array_bitfields(self, compname, output):
    """Keep digging until we hit a Register."""
    generate_bitfields(self.space, compname, output)
             
def generate_register_bitfields(self, compname, output):
    """Generate bitfields for the contained fields."""
    
    fields = [ptr.obj for ptr in self.space if ptr]
    
    if fields:
        print >>output, ""
        regname = compname + '_' + self['name']
        print >>output, maincomment.format('{0} Field Descriptions'.format(self['name']))
    
    for f in reversed(fields):
        if not isinstance(f, Field):
            raise OutputterError("Hit non-Field term inside of Register, can't deal with this.", self)
         
        fieldname = regname + '_' + f['name']
        enums = f.getChildren(Enum)
        
        comment = ['{0} - {1}'.format(fieldname, f.textDescription())]
        if enums:
            comment.append('Values:')
            for e in enums:
                comment.append('{0} - {1}'.format(e['name'], e.textDescription()))
        
        print >>output, subcomment.format("\n".join(comment))
        print >>output, ''
        
        print >>output, define(fieldname + '_LSB', f['offset'])
        if (f['size'] == 1 and not enums):
            print >>output, define( fieldname,
                                    '0x{0:08X}u'.format(1 << f['offset'])
                                    )
        else:
            print >>output, define( fieldname + '_MASK',
                                    '0x{0:08X}u'.format(((1 << f['size'])-1) << f['offset'])
                                    )
            print >>output, define( fieldname + '(x)',
                                    '(x) << {0}_LSB'.format(fieldname)
                                    )
            
            for e in enums:
                print >>output, define( fieldname + '_' + e['name'],
                                        '{0}({1})'.format(fieldname, e['value']))
                             
RegisterArray.generate_bitfields = generate_array_bitfields
Register.generate_bitfields = generate_register_bitfields
                                   
#
# We need to output Components, which will be a typedef and a mess o'
# #define for any bitfields.
#
                                   
def generate_single_component(comp, output, standalone = True):
    """
    Render down a component tree into a C header.
    
    Keyword Arguments:
    comp - A Component to generate the header from.
    
    output - A file-like object to write the results to
    
    standalone - If True (default), generate this component as if it's
    a standalone file, complete with #ifdef protection, #includes, etc.
    If False, skip all this.
    """

    header = detab("""
        {name} Register Map
        Defines the registers in the {name} component.
        {desc}
        Generated automatically from {source} on {time}
        Do not modify this file directly.
        """).format(
            name = comp['name'],
            desc = comp.textDescription(),
            source = comp.sourcefile,
            time = datetime.datetime.now().ctime()
        )

    print >>output, maincomment.format(header)

    if standalone:
        headername = make_header_filename('', comp.sourcefile).upper().replace('.', '_')
        header = detab("""
            #ifndef {hdr}
            #define {hdr}
            """).format(hdr = headername)
            
        print >>output, header, storage_class_definitions

    # Generate our typedef from the space
    print >>output, 'typedef struct {'
    generate_struct_innards(comp.space, '    ', output)
    print >>output, '}} t_{comp};'.format(comp = comp['name'])

    # Generate the bitfield definitions
    generate_bitfields(comp.space, comp['name'], output)
     
    if standalone:
        footer = detab("""
            #endif
            """)

        print >>output, footer


Component.generate = generate_single_component

########################################################################
# Routines for outputting MemoryMaps
########################################################################
    
#
# We need to output base addresses.
#
    
def generate_baseaddrs(space, basename, output):
    """Create base address definitions."""
    for ptr in itertools.ifilter(bool, space):
        # We don't actually care whether it's the base of an array or an instance
        desc = ptr.obj.textDescription()
        if desc:
            print >>output, subcomment.format(desc)
        
        print >>output, define(
            name = basename + '_' + ptr.obj['name'] + '_BASE',
            val = '{0}_BASE + 0x{1:08X}u'.format(basename, ptr.pos));
  
#
# We need to output structure pointers to peripherals.
#

def generate_peripherals(space, basename, output):
    """Create peripheral pointer definitions."""
    
    for ptr in itertools.ifilter(bool, space):
        ptr.obj.generate_peripherals(basename, output)
   
def generate_array_peripherals(self, basename, output):
    """Create peripheral definitions for an Array of Instances."""
    
    children = self.getChildren();
    
    if (len(children) > 1) or not isinstance(children[0], Instance):
        raise OutputterError("Instance Arrays allowed only for single Instances, not groups.", self)
        
    else:
        inst = self.children[0]
        pername = basename + '_' + inst['name']
        print >>output, '__attribute__((unused)) static t_{comp} * const {perarray:25s}= (t_{comp} *){per}_BASE;'.format(
                            comp = inst.binding['name'],
                            perarray = '{0}[{1}]'.format(pername, self['count']),
                            per = pername)
                            
def generate_instance_peripherals(self, basename, output):
    """Create a pointer to the peripheral."""
    pername = basename + '_' + self['name']
    print >>output, '__attribute__((unused)) static t_{comp:12s} * const {per:20s}= (t_{comp} *){per}_BASE;'.format(
                            comp = self.binding['name'],
                            per = pername)
    
    
InstanceArray.generate_peripherals = generate_array_peripherals
Instance.generate_peripherals = generate_instance_peripherals

#
# We need to output MemoryMaps, which will include all of the bound
# Components as either inline text or seperate #include files, plus
# base addressses and pointers to them.
#

def generate_memory_map(mmap, output=sys.stdout, external_reference = False):
    """Render down a memory map tree into a C header."""

    header = detab("""
        {name} Memory Map
        Defines all of the {name} components.
        {desc}
        Generated automatically from {source} on {time}
        Do not modify this file directly.
        """).format(
            name = mmap['name'],
            desc = mmap.textDescription(),
            source = mmap.sourcefile,
            time = datetime.datetime.now().ctime()
        )

    print >>output, maincomment.format(header)

    headername = make_header_filename('', mmap.sourcefile).upper().replace('.', '_')
    header = detab("""
        #ifndef {hdr}
        #define {hdr}
        
        """) + storage_class_definitions
    print >>output, header.format(hdr = headername)
    
    # Find all of the components referenced, uniquely
    references = set(inst.binding for inst in mmap.getChildren())
    
    if external_reference:
        # Include all of the referenced component headers.
        print >>output, ''
        for inst in references:
            linkname = make_header_filename('', inst.sourcefile)
            print >>output, '#include "{0}"'.format(linkname)
        print >>output, ''
    
    else:
        for comp in references:
            comp.generate(output = output, standalone = False)
            print >>output, ''
    
    # Wire up the instances to their locations in memory
    print >>output, maincomment.format("Peripheral memory map")
    print >>output, ""
    
    basename = mmap['name']
    baseaddr = mmap['base']
    print >>output, define(
                    name = basename + '_BASE',
                    val = '0x{0:08X}u'.format(baseaddr))
    
    generate_baseaddrs(mmap.space, basename, output)
    
    print >>output, ""
    print >>output, maincomment.format("Peripheral declaration")
    print >>output, ""
    
    generate_peripherals(mmap.space, basename, output)
    
    footer = detab("""
        #endif
        """)
        
    print >>output, footer

MemoryMap.generate = generate_memory_map

########################################################################
# Main code
########################################################################

def output_select(output_dir, sourcefile):
    """If output_dir is non-null, produce a context manager for
    an output file, with the name derived from output_dir and sourcefile.

    Otherwise, produce a context manager that simply wraps
    the standard output stream.

    """
    class StdoutWrapper:
        def __enter__(self):
            return sys.stdout

        def __exit__(self, type, value, traceback):
            return

    if output_dir:
        return open(make_header_filename(output_dir, sourcefile), 'w')

    else:
        return StdoutWrapper()

def main(argv=None):
    ########################################################
    # Start by parsing the command line options.
    ########################################################
    if argv is None:
        argv = sys.argv[1:]

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output-dir', '-o', help="""
        When present, specifies a directory to write output files to.
        Output files will have the same names as the input files they
        correspond to, but with a .vhd extension, rather than .xml.
        When absent, all output will be written to stdout.
        """)
    ap.add_argument('--no-mmap', '-m', action="store_false", dest = 'mmap', help="""
        Ignore any memory map files; generate only component
        files.
        """)
    ap.add_argument('--external-refs', '-e', action="store_true", dest = 'xrefs', help="""
        If generating a memory map, create all components as seperate
        header files and #include them all.  Default is to put all the
        declarations into one big header file.
        """)
    ap.add_argument('source', nargs = '+', help="""
        Input files in the HTI XML register or memory map description
        format.
        """)

    args = ap.parse_args(argv)

    if (args.output_dir and not os.path.isdir(args.output_dir)):
        print >>sys.stderr, "Unable to write to directory {0}".format(args.output_dir)
        sys.exit(1)

    ########################################################
    # Read all the XML files
    ########################################################

    parser = XmlReader()
    components = []
    maps = []
    codec = codecs.lookup('utf-8')[-1]

    for source in args.source:
        try:
            root = parser.Parse(source)
            if isinstance(root, Component):
                root.finish()
                components.append(root)
            
            elif isinstance(root, MemoryMap):
                maps.append(root)
                
        except:
            print >>sys.stderr, "Error parsing {0}".format(source)
            traceback.print_exc()

    ########################################################
    # Bind the output methods
    ########################################################
    
    HtiElement.textDescription = lambda self: "\n\n".join(self.getDescription())
    
    components_as_files = (not maps) or (not args.mmap) or (args.xrefs)
    
    ########################################################
    # And generate all of the outputs
    ########################################################

    if maps and args.mmap:
        cmap = MemoryMap.build_component_map(components)
        
        for mmap in maps:
            mmap.finish(cmap)
            
            with output_select(args.output_dir, mmap.sourcefile) as target:
                try:
                    mmap.generate(output = codec(target), external_reference = args.xrefs)
                except:
                    print >>sys.stderr, "Error parsing {0}, sourced from {1}".format(
                        mmap['name'], mmap.sourcefile)
                    traceback.print_exc()
                    
    if components_as_files:
        for comp in components:
            with output_select(args.output_dir, comp.sourcefile) as target:
                try:
                    comp.generate(output = codec(target), standalone = True)

                except OutputterError as e:
                    print >>sys.stderr, e.args[0]
                    print >>sys.stderr, '    Sourced from {0}, line {1}'.format(
                                        e.args[1].sourcefile,
                                        e.args[1].sourceline)
                        
                except:
                    print >>sys.stderr, "Unknown error parsing {0}, sourced from {1}".format(
                        comp['name'], comp.sourcefile)
                    traceback.print_exc()

if __name__ == "__main__":
    sys.exit(main())