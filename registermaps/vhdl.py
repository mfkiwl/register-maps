"""Translate XML register definitions into VHDL."""

# There's a real challenge in all this code in making it all clean.  Ideally
# everything we do in here could be implemented by Visitors calling Visitors
# endlessly all the way down the line, and no ancillary data structures.  This
# is conceptually clean, but in practice means
#   a) a LOT of walks through the same tree; and
#   b) that the code is never linear, it all winds up scattered through a
#       hundred small classes doing small things.
#
# Ancillary structures help with this, but make everything ad-hoc where they're
# used.  So I've tried to strike a balance here of using ad-hoc structures to
# do simple things while allowing Visitors to do the more complex work, but
# that balance is definitely more art than science.

import os
import os.path
import textwrap
import datetime

from . import xml_parser, resource_text, printverbose
from .visitor import Visitor

wrapper = textwrap.TextWrapper()

def dedent(text):
    """Unindents a triple quoted string, stripping up to 1 newline from
    each edge."""
    
    if text.startswith('\n'):
        text = text[1:]
    if text.endswith('\n'):
        text = text[:-1].rstrip('\t ')
    return textwrap.dedent(text)

def register_format(element, index=True):
    if element.width == 1:
        return 'std_logic'
    
    fmt = {
        'bits' : 'std_logic_vector',
        'signed' : 'signed',
        'unsigned' : 'unsigned'
    }[element.format]
    
    if index:
        return '{0}({1} downto 0)'.format(fmt, element.width-1)
    else:
        return fmt
        
def commentblock(text):
    """Turn a multi-line text block into a multi-line comment block.
    
    Paragraphs in the source text are separated by newlines.
    """
    
    wrapper = textwrap.TextWrapper(
        width = 76,
        replace_whitespace = False,
        drop_whitespace = False
    )
    
    cbar = '-' * (wrapper.width + 4)
    paragraphs = text.splitlines()
    wraplists = (wrapper.wrap(p) if p else [''] for p in paragraphs)
    lines = (line for thislist in wraplists for line in thislist)
    return (
        cbar + '\n' + 
        '\n'.join('--  ' + x for x in lines) + '\n' + 
        cbar
    )

#######################################################################
# Helper visitors
#######################################################################

class FixReservedWords(Visitor):
    """Modify the tree for legal VHDL.
    
    Any names that are with illegal VHDL characters will be changed.
    
    All nodes will be given a .identifier that is related the name, but
    has a _0 appended if the name is a reserved word.
    
    Return a list of the changes, each change is (type, old, new), such as
    ('register identifier', 'MONARRAY.OUT', 'MONARRAY.OUT_0').
    """
    
    # VHDL identifiers must start with a letter and cannot end with an
    # underscore.  We'll take our definitions of letters and digits straight
    # from the 2008 LRM §15.2.  Note there is no uppercase equivalent
    # for ß or ÿ.
    
    uppercase = 'ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞ'
    lowercase = 'abcdefghijklmnopqrstuvwxyzßàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ'
    letters = uppercase + lowercase
    word_chars = letters + '0123456789_'
    
    # Again from the 2008 LRM, §15.10.
    reserved_words = set("""
        abs fairness nand select
        access file new sequence
        after for next severity
        alias force nor signal
        all function not shared
        and null sla
        architecture generate sll
        array generic of sra
        assert group on srl
        assume guarded open strong
        assume_guarantee or subtype
        attribute if others
        impure out then
        begin in to
        block inertial package transport
        body inout parameter type
        buffer is port
        bus postponed unaffected
        label procedure units
        case library process until
        component linkage property use
        configuration literal protected
        constant loop pure variable
        context vmode
        cover map range vprop
        mod record vunit
        default register
        disconnect reject wait
        downto release when
        rem while
        else report with
        elsif restrict
        end restrict_guarantee xnor
        entity return xor
        exit rol
        ror""".split()
    )
    
    def invalidvhdl(self, name):
        """Return None if name is valid VHDL, otherwise a new name that is."""
    
        # First character must be a letter
        it = iter(name)
        first = next(it)
        if first not in self.letters:
            raise ValueError("{} {} does not start with valid letter.".format(
                ntype, node.name
            ))
        newchars = [first]
        
        # Later characters can be anything
        changed = False
        char = first
        for char in it:
            if char not in self.word_chars:
                changed = True
                char = '_'
            newchars.append(char)
            
        # But we can't end with an underline
        if char == '_':
            newchars.append('_0')
            changed = True
            
        if changed:
            return ''.join(newchars)
        else:
            return None
    
    def defaultvisit(self, node):
        """All nodes behave the same."""
        
        ntype = type(node).__name__.lower()
        
        # First off, check to see if it's even a valid VHDL identifier.
        oldname = node.name
        newname = self.invalidvhdl(oldname)
        
        # If it wasn't even valid VHDL it can't be a reserved word.
        if newname:
            node.name = node.identifier = newname
            changes = [(ntype + ' name', oldname, newname)]
        
        # Reserved words need a new identifier but not a new name
        elif node.name.lower() in self.reserved_words:
            newname = node.name + '_0'
            node.identifier = newname
            changes = [(ntype + ' identifier', node.name, newname)]
            
        # All good here
        else:
            node.identifier = newname = oldname
            changes = []
        
        # Sweep the children too
        changes.extend(
            (ctype, oldname + '.' + old, newname + '.' + new)
                for childchanges in self.visitchildren(node)
                for ctype, old, new in childchanges
        )
        return changes
        
    def visit_Enum(self, node):
        return []

class GenerateAddressConstants(Visitor):
    """Print address constants into the package header."""
        
    def begin(self, startnode):
        self.body = []
    
    def visit_Component(self, node):
        maxaddr = node.size - 1
        self.print(commentblock('Address Constants'))
        self.printf('subtype t_addr is integer range 0 to {};', maxaddr)
        
        self.visitchildren(node)
        self.print('function GET_ADDR(address: std_logic_vector) return t_addr;')
        self.print('function GET_ADDR(address: unsigned) return t_addr;')
        
    def visit_RegisterArray(self, node):
        consts = (
            ('_BASEADDR', node.offset),
            ('_LASTADDR', node.offset+node.size-1),
            ('_FRAMESIZE', node.framesize),
            ('_FRAMECOUNT', node.framesize)
        )
        for name, val in consts:
            self.printaddress(node.name+name, val)
        self.visitchildren(node)
        
    def visit_Register(self, node):
        self.printaddress(node.name+'_ADDR', node.offset)
        
    def visit_MemoryMap(self, node):
        pass
        
    def printaddress(self, name, val):
        self.printf('constant {}: t_addr := {};', name, val)

class GenerateTypes(Visitor):
    """Go through the HtiComponent tree generating register types.
    
    Immediately outputs:
    - Register types
    - Register array types
    - Enumeration constants
    
    """
    
    def begin(self, startnode):
        self.body = []
    
    def namer(self, node):
        """Returns the appropriate type name for a given node."""
        if isinstance(node, xml_parser.Register):
            return 't_' + node.name
        elif isinstance(node, xml_parser.RegisterArray):
            return 'ta_' + node.name
        else:
            raise TypeError('unable to name {}', type(node).__name__)
    
    def visit_Component(self, node):
        """Provide the header for the component-level file."""
        
        self.print(commentblock('Register Types'))
        self.printf('subtype t_busdata is std_logic_vector({} downto 0);', node.width-1)
        
        # First define all the registers and registerarrays
        self.visitchildren(node)
        
        # Now create a gestalt structure for the entire register file.
        self.printf('type t_{}_regfile is record', node.name)
        for child, _, _ in node.space.items():
            self.printf('    {}: {};', child.identifier, self.namer(child))
        self.printf('end record t_{}_regfile;', node.name)
        
    def visit_RegisterArray(self, node):
        """Generate the array type and, if necessary, a subsidiary record."""
        
        # First define the register types.
        self.visitchildren(node)
        
        # Now we can define the array type.
        fields = [
            (child.identifier, self.namer(child))
                for child, _, _ in node.space.items()
        ]
        if len(fields) == 1:
            # Don't make a structured type from this array.
            basename = fields[0][1]
        else:
            basename = 'tb_' + node.name
            self.printf('type {} is record', basename)
            for fn, ft in fields:
                self.printf('    {}: {};'.format(fn, ft))
            self.printf('end record {};', basename)
                    
        self.printf('type {} is array({} downto 0) of {};',
            self.namer(node), node.size-1, basename
        )
        self.print()
        
    def visit_Register(self, node):
        """Generate the register types and enumeration constants."""
        if node.space:
            # We're a complex register
            with self.tempvars(enumlines=[], registername=node.name):
                self.printf('type t_{name} is record', name=node.name)
                self.visitchildren(node)
                self.printf('end record t_{name};', name=node.name)
            
                for line in self.enumlines:
                    self.print(line)
        
        else:
            # We're a simple register
            self.printf('subtype t_{name} is {fmt};',
                name=node.name,
                fmt = register_format(node),
            )
                    
    def visit_Field(self, node):
        """Generate record field definitions, and gather enumeration constants."""
        
        with self.tempvars(field=node, fieldtype=register_format(node)):
            self.printf('    {}: {};', node.identifier, self.fieldtype)
            self.visitchildren(node)
        
    def visit_Enum(self, node):
        """Push enumeration values into the enum list."""
        
        enumname = self.registername + '_' + self.field.name + '_' + node.name
        self.enumlines.append(
            'constant {}: {} := "{:0{}b}";'.format(
                enumname, self.fieldtype, node.value, self.field.width
        ))
            
    def visit_MemoryMap(self, node):
        pass

class GenerateFunctionDeclarations(Visitor):
    """Print function declaration statements for the package header."""
    
    def visit_Component(self, node):
        self.print(commentblock('Accessor Functions'))
        self.visitchildren(node)
        self.printf(dedent("""
            procedure UPDATE_REGFILE(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                variable reg: inout t_{name}_regfile;
                success: out boolean);
            procedure UPDATESIG_REGFILE(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                signal reg: inout t_{name}_regfile;
                success: out boolean);
            procedure READ_REGFILE(
                offset: in t_addr;
                reg: in t_{name}_regfile;
                dat: out t_busdata;
                success: out boolean);
            """), name=node.name
        )
            
    def visit_RegisterArray(self, node):
        self.printf(dedent("""
            procedure UPDATE_{name}(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                variable ra: inout ta_{name};
                success: out boolean);
            procedure UPDATESIG_{name}(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                signal ra: inout ta_{name};
                success: out boolean);
            procedure READ_{name}(
                offset: in t_addr;
                ra: in ta_{name};
                dat: out t_busdata;
                success: out boolean);
            """), name=node.name
        )
        self.visitchildren(node)
    
    def visit_Register(self, node):
        # Register access functions
        self.printf(dedent("""
            function DAT_TO_{name}(dat: t_busdata) return t_{name};
            function {name}_TO_DAT(reg: t_{name}) return t_busdata;
            procedure UPDATE_{name}(
                dat: in t_busdata; byteen: in std_logic_vector;
                variable reg: inout t_{name});
            procedure UPDATESIG_{name}(
                dat: in t_busdata; byteen: in std_logic_vector;
                signal reg: inout t_{name});
            """), name=node.name
        )
        
class GenerateFunctionBodies(Visitor):
    """Print function bodies for the package body."""
    
    def visit_Component(self, node):
        """Print all function bodies for the Component"""
        
        self.print(commentblock('Address Grabbers'))
        maxaddr = (node.size * node.width // 8) - 1
        high = maxaddr.bit_length() - 1;
        self.printf(dedent("""
            function GET_ADDR(address: std_logic_vector) return t_addr is
                variable normal : std_logic_vector(address'length-1 downto 0);
            begin
                normal := address;
                return TO_INTEGER(UNSIGNED(normal({high} downto 0)));
            end function GET_ADDR;
            
            function GET_ADDR(address: unsigned) return t_addr is
            begin
                return TO_INTEGER(address({high} downto 0));
            end function GET_ADDR;
            
            """), high=high
        )
        
        self.print(commentblock('Accessor Functions'))
        self.visitchildren(node)
        self.printf('---- Complete Register File ----', name=node.name)
        
        self._regfileupdate(node, False)
        self._regfileupdate(node, True)
        self._regfileread(node)
    
    @staticmethod
    def _fnclass(sig):
        """UPDATE or UPDATESIG_"""
        if sig:
            return {'fn' : 'UPDATESIG', 'class' : 'signal'}
        else:
            return {'fn' : 'UPDATE', 'class' : 'variable'}
    
    def _updatewhenlines(self, node, base, offset, sig):
        """Text block of lines to go in an UPDATE_ function case block.
        
        Readable addresses set dat correctly, otherwise clear success.
        
        base is 'reg' or 'ra(idx)'
        offset is 'offset' or 'offs'
        sig is True for UPDATESIG_ and False for UPDATE_
        """
        
        params = self._fnclass(sig)
        params['base'] = base
        params['offset'] = offset
        
        whenlines = []
        gaps = False
        for obj, _, _ in node.space.items():
            # Is this a thing we can't write to?
            if (not obj) or obj.readOnly:
                gaps = True
                continue
                
            # Create a line of how to write to this thing, cook in the
            # formatting, and hold onto it for a bit.
            if isinstance(obj, xml_parser.Register):
                line = "when {name}_ADDR => {fn}_{name}(dat, byteen, {base}.{identifier});"
                    
            elif isinstance(obj, xml_parser.RegisterArray):
                line = (
                    "when {name}_BASEADDR to {name}_LASTADDR => "
                    "{fn}_{name}(dat, byteen, {offset}-{name}_BASEADDR, {base}.{identifier}, success);"
                )
                
            else:
                raise ValueError('Child node {}: {} of Component {}'.format(
                    type(obj).__name__, obj.name, node.name
                ))
                
            whenlines.append(dedent(line).format(
                name=obj.name, identifier=obj.identifier, **params)
            )
            
        # Put an others line on if the case wasn't filled.
        if gaps:
            whenlines.append("when others => success := false;")
            
        # Indent all those lines and slam them together into a multi-line block.
        return '\n'.join('        ' + w for w in whenlines)
    
    def _readwhenlines(self, node, base, offset):
        """Text block of lines to go in a READ function case block.
        
        Readable addresses set dat correctly, otherwise clear success.
        
        base is 'reg' or 'ra(idx)'
        offset is 'offset' or 'offs'
        """
        
        whenlines = []
        gaps = False
        for obj, _, _ in node.space:
            # Is this a thing we can't read from?
            if (not obj) or obj.writeOnly:
                gaps = True
                continue
                
            # Create a line of how to read from this thing, cook in the
            # formatting, and hold onto it for a bit.
            if isinstance(obj, xml_parser.Register):
                line = "when {name}_ADDR => dat := {name}_TO_DAT({base}.{identifier});"
                    
            elif isinstance(obj, xml_parser.RegisterArray):
                line = (
                    "when {name}_BASEADDR to {name}_LASTADDR => "
                    "READ_{name}({offset}-{name}_BASEADDR, {base}.{identifier}, dat, success);"
                )
                
            else:
                raise ValueError('Child node {}: {} of {}: {}'.format(
                    type(obj).__name__, obj.name, type(self).__name__, node.name
                ))
                
            whenlines.append(
                dedent(line).format(
                    name=obj.name, identifier=obj.identifier,
                    base=base, offset=offset
            ))
            
        # Put an others line on if the case wasn't filled.
        if gaps:
            whenlines.append("when others => success := false;")
            
        # Indent all those lines and slam them together into a multi-line block.
        return '\n'.join('        ' + w for w in whenlines)
        
    def _regfileupdate(self, node, sig):
        """Print either UPDATE_ or UPDATESIG_ for the whole register file"""
        
        params = self._fnclass(sig)
        whenlines = self._updatewhenlines(node, 'reg', 'offset', sig)
        self.printf(dedent("""
            procedure {fn}_REGFILE(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                {class} reg: inout t_{name}_regfile;
                success: out boolean
            ) is
            begin
                success := true;
                dat := (others => 'X');
                case offset is
            {whenlines}
                end case;
            end procedure {fn}_REGFILE;
            """), name=node.name, whenlines=whenlines, **params
        )
        
    def _regfileread(self, node):
        """Print READ_ for the whole register file."""
        
        whenlines = self._readwhenlines(node, 'reg', 'offset')
        self.printf(dedent("""
            procedure READ_REGFILE(
                offset: in t_addr;
                reg: in t_{name}_regfile;
                dat: out t_busdata;
                success: out boolean
            ) is
            begin
                success := true;
                case offs is
            {whenlines}
                end case;
            end procedure READ_{name};
            """), name=node.name, whenlines = whenlines
        )
        
    def visit_RegisterArray(self, node):
        """Register array access function bodies."""

        self.visitchildren(node)
        self.printf('---- {name} ----', name=node.name)
        if len(node.space) == 1: 
            self._simpleregarrayupdate(node, False)
            self._simpleregarrayupdate(node, True)
            self._simpleregarrayread(node)
        else:
            self._complexregarrayupdate(node, False)
            self._complexregarrayupdate(node, True)
            self._complexregarrayread(node)
            
    def _complexregarrayupdate(self, node, sig):
        """Generate either UPDATE_ or UPDATESIG_ for a hetrogynous RegisterArray"""
        
        params = self._fnclass(sig)
        whenlines = self._updatewhenlines(node, 'ra(idx)', 'offs', sig)
        self.printf(dedent("""
            procedure {fn}_{name}(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                {class} ra: inout ta_{name};
                success: out boolean
            ) is
                variable idx: integer range 0 to {name}_FRAMECOUNT-1;
                variable ofs: integer range 0 to {name}_FRAMESIZE-1;
            begin
                idx := offset / {name}_FRAMESIZE;
                offs := offset mod {name}_FRAMESIZE;
                success := true;
                dat := (others => 'X');
                case offs is
            {whenlines}
                end case;
            end procedure {fn}_{name};
            """), name=node.name, whenlines=whenlines, **params
        )
        
    def _complexregarrayread(self, node):
        """Generate a READ procedure for a hetrogynous RegisterArray."""
        
        whenlines = self._readwhenlines(node, 'ra(idx)', 'offs')
        self.printf(dedent("""
            procedure READ_{name}(
                offset: in t_addr;
                ra: in ta_{name};
                dat: out t_busdata;
                success: out boolean
            ) is
                variable idx: integer range 0 to {name}_FRAMECOUNT-1;
                variable ofs: integer range 0 to {name}_FRAMESIZE-1;
            begin
                idx := offset / {name}_FRAMESIZE;
                offs := offset mod {name}_FRAMESIZE;
                success := true;
                case offs is
            {whenlines}
                end case;
            end procedure READ_{name};
            """), name=node.name, whenlines = whenlines
        )
        
    def _simpleregarrayupdate(self, node, sig):
        """Print either UPDATE_ or UPDATESIG_ for a homogynous RegisterArray"""
        
        params = self._fnclass(sig)
        child, _, _ = next(node.space.items())
        if child.readOnly:
            filling = ['success := false;']
        else:
            filling = [
                'idx := offset / {name}_FRAMESIZE;',
                'success := true;',
                '{fn}_{child}(dat, byteen, ra(idx));'
            ]
        filling = '\n'.join('    ' + line for line in filling)
            
        self.printf(dedent("""
            procedure {fn}_{name}(
                dat: in t_busdata; byteen : in std_logic_vector;
                offset: in t_addr;
                {class} ra: inout ta_{name};
                success: out boolean
            ) is
                variable idx: integer range 0 to {name}_FRAMECOUNT-1;
            begin
            {filling}
            end procedure {fn}_{name};
            """), name=node.name, child=child.name, filling=filling, **params
        )
        
    def _simpleregarrayread(self, node):
        """Print the READ_ code for a homogynous RegisterArray"""
        
        child, _, _ = next(node.space.items())
        if child.writeOnly:
            filling = [
                "dat := (others => 'X')",
                'success := false;'
            ]
        else:
            filling = [
                'idx := offset / {name}_FRAMESIZE;',
                'success := true;',
                'dat := {child}_TO_DAT(ra(idx));'
            ]
        filling = '\n'.join('    ' + line for line in filling)
        
        self.printf(dedent("""
            procedure READ_{name}(
                offset: in t_addr;
                ra: in ta_{name};
                dat: out t_busdata;
                success: out boolean
            ) is
                variable idx: integer range 0 to {name}_FRAMECOUNT-1;
            begin
            {filling}
            end procedure READ_{name};
            """), name=node.name, child=child.name, filling=filling
        )
        
    def visit_Register(self, node):
        """Print register access function bodies."""
        
        self.printf(dedent("""
            ---- {name} ----
            function DAT_TO_{name}(dat: t_busdata) return t_{name} is
            begin"""), name=node.name
        )
        GenerateD2R(self.output).execute(node)
        self.printf(dedent("""
            end function DAT_TO_{name};
            
            function {name}_TO_DAT(reg: t_{name}) return t_busdata is
                variable ret: t_busdata := (others => '0');
            begin"""), name=node.name
        )
        GenerateR2D(self.output).execute(node)
        self.printf(dedent("""
                return ret;
            end function {name}_TO_DAT;
            
            procedure UPDATE_{name}(
                dat: in t_busdata; byteen: in std_logic_vector;
                variable reg: inout t_{name}) is
            begin"""), name=node.name
        )
        GenerateRegUpdate(self.output).execute(node)
        self.printf(dedent("""
            end procedure UPDATESIG_{name};
            
            procedure UPDATESIG_{name}(
                dat: in t_busdata; byteen: in std_logic_vector;
                signal reg: inout t_{name}
            ) is
                variable r : t_{name};
            begin
                r := reg;
                UPDATE_{name}(dat, byteen, r);
                reg <= r;
            end procedure UPDATESIG_{name};
            """), name=node.name
        )

class RegisterFunctionGenerator(Visitor):
    """ABC for function body iterator builders."""
    
    def visit_Register(self, node):
        if node.space:
            return self.complexRegister(node)
        else:
            return self.simpleRegister(node)
     
class GenerateD2R(RegisterFunctionGenerator):
    """Iterable of lines for the function body for DAT_TO_register."""
     
    def simpleRegister(self, node):
        if node.width == 1:
            line = '    return {fmt}(dat(0));'
        else:
            line = '    return {fmt}(dat({high} downto 0));'
        self.printf(line,
            name=node.name, fmt=register_format(node, False).upper(),
            high=node.width-1
        )
        
    def complexRegister(self, node):
        childlines = ',\n'.join(
            '        ' + line 
                for line in self.visitchildren(node)
        )
        self.printf(dedent("""
            return (
        {childlines}
            );"""),
            name=node.name, childlines=childlines
        )
        
    def visit_Field(self, node):
        """Return field lines."""
        if node.width == 1:
            line = '{name} => dat({high})'
        else:
            line = '{name} => {fmt}(dat({high} downto {low}))'
        return line.format(
            name=node.name, fmt=register_format(node, False).upper(),
            high=node.offset+node.width-1, low=node.offset
        )
        
class GenerateR2D(RegisterFunctionGenerator):
    """Iterable of lines for the function body for register_TO_DAT."""
    
    def simpleRegister(self, node):
        if node.width == 1:
            line = '    ret(0) := reg.{identifier};'
        else:
            line = '    ret({high} downto 0) := STD_LOGIC_VECTOR(reg.{identifier});'
        self.printf(line, identifier=node.identifier, high=node.width-1)
        
    def complexRegister(self, node):
        self.visitchildren(node)
    
    def visit_Field(self, node):
        if node.width == 1:
            line = '    ret({high}) := reg.{identifier};'
        else:
            line = '    ret({high} downto {low}) := STD_LOGIC_VECTOR(reg.{identifier});'
        self.printf(line,
            identifier=node.identifier, high=node.offset+node.width-1, low=node.offset
        )
        
class GenerateRegUpdate(RegisterFunctionGenerator):
    """Iterable of lines for the function body for UPDATE_register."""
    
    def simpleRegister(self, node):
        fmt = register_format(node, False).upper()
        for bit, start in enumerate(range(0, node.width, 8)):
            end = min(start+7, node.width)
            if start == end:
                line = 'reg({L}) := dat({L});'
            else:
                line = 'reg({H} downto {L}) := {fmt}(dat({H} downto {L}));'
            
            self.printf('    if byteen({}) then', (bit))
            self.printf('        ' + line, fmt = fmt, H = end, L = start)
            self.print( '    end if;')
            
    def complexRegister(self, node):
        for bit, start in enumerate(range(0, node.width, 8)):
            subspace = node.space[start:start+8]
            if not any(obj for obj, _, _ in subspace):
                continue
                
            self.printf('    if byteen({}) then', bit)
            for obj, start, size in subspace:
                if not obj:
                    continue
                if size > 1 or obj.size > 1:
                    # This field is indexable.
                    line = 'reg.{identifier}({fh} downto {fl}) := {fmt}(dat({dh} downto {dl}));'
                else:
                    # This field is a bit.
                    line = 'reg.{identifier} := dat({dl});'
                self.printf('        ' + line,
                    fh = start+size-1-obj.offset,
                    fl = start-obj.offset,
                    dh = start+size-1,
                    dl = start,
                    fmt = register_format(obj, False).upper(),
                    identifier=obj.identifier
                )
            self.print( '    end if;')

#######################################################################
# Main visitors
#######################################################################

class basic(Visitor):
    """Basic VHDL output.
    
    This output makes no assumptions about what the bus type is, and expects
    no support packages to be available.
    """
    
    extension = '.vhd'
    encoding = 'iso-8859-1'
    
    component_fileheader = dedent("""
    {name} Register Map
    
    Defines the registers in the {name} component.
    
    {desc}{changes}
    
    Generated automatically from {source} on {time:%d %b %Y %H:%M}
    
    Do not modify this file directly.  See README.rst for details.
    """)
    
    use_packages = [
        'ieee.std_logic_1164.all',
        'ieee.numeric_std.all'
    ]
    
    def begin(self, startnode):
        changer = FixReservedWords()
        changed_nodes = changer.execute(startnode)
        if changed_nodes:
            changes =  (
                'Changes from XML:\n' +
                '\n'.join(
                    '    {0[0]}: {0[1]} -> {0[2]}'.format(c) for c in changed_nodes
            ))
            printverbose(changes)
            self.changes = '\n\n' + changes
        else:
            self.changes = ''
        
    def printlibraries(self):
        packages = sorted(self.use_packages)
        lib = None
        for p in packages:
            newlib = p.split('.', maxsplit=1)[0]
            if newlib != lib:
                lib = newlib
                if lib != 'work':
                    self.printf('library {};', lib)
            self.printf('use {};', p)
    
    def visit_Component(self, node):
        """Create a VHDL file for a Component."""
        
        # Comments, libraries, and boilerplate.
        self.pkgname = 'pkg_' + node.name
        self.print(commentblock(
            self.component_fileheader.format(
                name = node.name,
                desc = '\n\n'.join(node.description),
                source = node.sourcefile,
                time = datetime.datetime.now(),
                pkg = self.pkgname,
                changes = self.changes
        )))
        self.print()
        self.printlibraries()
        self.print()
        
        self.printf("package {pkg} is", pkg=self.pkgname)
        
        # Address Constants
        GenerateAddressConstants(self.output).execute(node)
        self.print()
        
        # Types
        GenerateTypes(self.output).execute(node)
        self.print()
        
        # Type conversion declarations
        GenerateFunctionDeclarations(self.output).execute(node)
        
        self.printf(dedent("""
            end package {pkg};
            ------------------------------------------------------------------------
            package body {pkg} is
            """), pkg=self.pkgname
        )
        
        # Type conversion functions
        GenerateFunctionBodies(self.output).execute(node)
        
        self.printf("end package body {pkg};", pkg=self.pkgname)
        self.print()
        
    def visit_MemoryMap(self, node):
        pass
    
    @classmethod
    def preparedir(kls, directory):
        """Copy the README.rst file into the target directory."""
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, 'README.rst')
        printverbose(target)
        with open(target, 'w') as f:
            f.write(resource_text('resource/vhdl.basic/README.rst'))
    
