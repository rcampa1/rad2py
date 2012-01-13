#!/usr/bin/env python
# coding:utf-8

"Queues(Pipe)-based independent remote client-server Python Debugger"

__author__ = "Mariano Reingart (reingart@gmail.com)"
__copyright__ = "Copyright (C) 2011 Mariano Reingart"
__license__ = "GPL 3.0"

# remote debugger queue-based (jsonrpc-like interface)
# based on idle, inspired by pythonwin implementation

import bdb
import linecache
import os
import sys
import traceback
import cmd


class Qdb(bdb.Bdb):
    "Qdb Debugger Backend"

    def __init__(self, pipe):
        bdb.Bdb.__init__(self)
        self.frame = None
        self.interacting = 0
        self.waiting = False
        self.pipe = pipe # for communication
        self.start_continue = True # continue on first run
        self._wait_for_mainpyfile = False
        self._lineno = None     # last listed line numbre
        # replace system standard input and output (send them thru the pipe)
        sys.stdin = self
        sys.stdout = self

    # Override Bdb methods

    def user_call(self, frame, argument_list):
        """This method is called when there is the remote possibility
        that we ever need to stop in this function."""
        if self._wait_for_mainpyfile:
            return
        if self.stop_here(frame):
            print >>self.stdout, '--Call--'
            self.interaction(frame, None)
   
    def user_line(self, frame):
        """This function is called when we stop or break at this line."""
        if self._wait_for_mainpyfile:
            if (self.mainpyfile != self.canonic(frame.f_code.co_filename)
                or frame.f_lineno<= 0):
                return
            self._wait_for_mainpyfile = 0
        self.interaction(frame)

    def user_exception(self, frame, info):
        if self._wait_for_mainpyfile:
            return
        ##print info
        extype, exvalue, trace = info
        # pre-process stack trace as it isn't pickeable (cannot be sent pure)
        trace = traceback.extract_tb(trace)
        msg = {'method': 'except_hook', 'args':(extype, exvalue, trace)}
        self.pipe.send(msg)
        self.interaction(frame, info)

    def run(self, code, interp=None, *args, **kwargs):
        try:
            self.interp = interp
            self.interacting = self.start_continue and 1 or 2
            return bdb.Bdb.run(self, code, *args, **kwargs)
        finally:
            self.interacting = 0

    def runcall(self, function, interp=None, *args, **kwargs):
        try:
            self.interp = interp
            self.interacting = self.start_continue and 1 or 2
            return bdb.Bdb.runcall(self, function, *args, **kwargs)
        finally:
            self.interacting = 0

    def _runscript(self, filename):
        # The script has to run in __main__ namespace (clear it)
        import __main__
        __main__.__dict__.clear()
        __main__.__dict__.update({"__name__"    : "__main__",
                                  "__file__"    : filename,
                                  "__builtins__": __builtins__,
                                 })

        # avoid stopping before we reach the main script 
        self._wait_for_mainpyfile = 1
        self.mainpyfile = self.canonic(filename)
        self._user_requested_quit = 0
        statement = 'execfile( "%s")' % filename
        self.run(statement)

    # General interaction function

    def interaction(self, frame, info=None):
        code, lineno = frame.f_code, frame.f_lineno
        filename = code.co_filename
        basename = os.path.basename(filename)
        message = "%s:%s" % (basename, lineno)
        if code.co_name != "?":
            message = "%s: %s()" % (message, code.co_name)
        #  sync_source_line()
        if frame and filename[:1] + filename[-1:] != "<>" and os.path.exists(filename):
            # notify debugger
            line = linecache.getline(filename, lineno,
                                     frame.f_globals)
            self.pipe.send({'method': 'debug_event', 'args': (filename, lineno, line)})

        # wait user events 
        self.waiting = True    
        self.frame = frame
         # save and change interpreter namespaces to the current frame
        ## frame.f_locals
        # copy globals into interpreter, so them can be inspected 
        ## frame.f_globals
        try:
            while self.waiting:
                self.pipe.send({'method': 'interaction', 'args': ()})
                ##print ">>>",
                request = self.pipe.recv()
                ##print request
                response = {'version': '1.1', 'id': request.get('id'), 
                            'result': None, 
                            'error': None}
                try:
                    # dispatch message (JSON RPC like)
                    method = getattr(self, request['method'])
                    response['result'] = method.__call__(*request['args'], 
                                                **request.get('kwargs', {}))
                except Exception, e:
                    response['error'] = {'code': 0, 'message': str(e)}
                self.pipe.send(response)

        finally:
            self.waiting = False
        self.frame = None

    # Command definitions, called by interaction()

    def do_continue(self):
        self.set_continue()
        self.waiting = False

    def do_step(self):
        self.set_step()
        self.waiting = False

    def do_return(self):
        self.set_return(self.frame)
        self.waiting = False

    def do_next(self):
        self.set_next(self.frame)
        self.waiting = False

    def do_quit(self):
        self.set_quit()
        self.waiting = False

    def do_jump(self, lineno):
        arg = int(lineno)
        try:
            self.frame.f_lineno = arg
        except ValueError, e:
            print '*** Jump failed:', e
            return False

    def do_list(self, arg):
        last = None
        if arg:
            if isinstance(arg, tuple):
                first, last = arg
            else:
                first = arg
        elif not self._lineno:
            first = max(1, self.frame.f_lineno - 5)                        
        else:
            first = self._lineno + 1
        if last is None:
            last = first + 10
        filename = self.frame.f_code.co_filename
        breaklist = self.get_file_breaks(filename)
        for lineno in range(first, last+1):
            line = linecache.getline(filename, lineno,
                                     self.frame.f_globals)
            if not line:
                print '[EOF]'
                break
            else:
                breakpoint = "B" if lineno in breaklist else ""
                current = "->" if self.frame.f_lineno == lineno else ""
                self.pipe.send({'method': 'show_line', 'args': (filename, lineno, breakpoint, current, line, )})
                self._lineno = lineno

    def do_set_breakpoint(self, filename, lineno, temporary=0):
        self.set_break(self.canonic(filename), lineno, temporary)

    def do_clear_breakpoint(self, filename, lineno):
        self.clear_break(filename, lineno)

    def do_clear_file_breakpoints(self, filename):
        self.clear_all_file_breaks(filename)

    def do_clear(self, arg):
        # required by BDB to remove temp breakpoints!
        err = self.clear_bpbynumber(arg)
        if err:
            print '*** DO_CLEAR failed', err

    def do_inspect(self, arg):
        return eval(arg, self.frame.f_globals,
                    self.frame.f_locals)

    def do_exec(self, arg):
        code = compile(arg + '\n', '<stdin>', 'single')
        exec code in self.frame.f_globals, self.frame.f_locals

    def displayhook(self, obj):
        """Custom displayhook for the do_exec which prevents
        assignment of the _ variable in the builtins.
        """
        # reproduce the behavior of the standard displayhook, not printing None
        if obj is not None:
            self.pipe.send({'method': 'display_hook', 'args':  repr(obj)})

    def reset(self):
        bdb.Bdb.reset(self)
        self.waiting = False
        self.frame = None

    def post_mortem(self, t=None):
        # handling the default
        if t is None:
            # sys.exc_info() returns (type, value, traceback) if an exception is
            # being handled, otherwise it returns None
            t = sys.exc_info()[2]
            if t is None:
                raise ValueError("A valid traceback must be passed if no "
                                 "exception is being handled")
        self.reset()
        # get last frame:
        while t is not None:
            frame = t.tb_frame
            t = t.tb_next
            #print frame, t
            #print frame.f_code, frame.f_lineno
            code, lineno = frame.f_code, frame.f_lineno
            filename = code.co_filename
            line = linecache.getline(filename, lineno)
            current = "->" if t is None else ""
            self.pipe.send({'method': 'show_line', 'args': (filename, lineno, "", current, line, )})

        self.interaction(frame)

    # console file-like object emulation
    def readline(self):
        "Replacement for stdin.readline()"
        msg = {'method': 'readline', 'args': ()}
        self.pipe.send(msg)
        msg = self.pipe.recv()
        return msg['result']

    def readlines(self):
        "Replacement for stdin.readlines()"
        lines = []
        while lines[-1:] != ['\n']:
            lines.append(self.readline())
        return lines

    def write(self, text):
        "Replacement for stdout.write()"
        msg = {'method': 'write', 'args': (text, )}
        self.pipe.send(msg)
        
    def writelines(self, l):
        map(self.write, l)

    def flush(self):
        pass

    def isatty(self):
        return 0


class QueuePipe(object):
    "Simulated pipe for threads (using two queues)"
    
    def __init__(self, name, in_queue, out_queue):
        self.__name = name
        self.in_queue = in_queue
        self.out_queue = out_queue

    def send(self, data):
        print self.__name, "send", data
        self.out_queue.put(data, block=True)
        print self.__name, "joined"

    def recv(self, count=None, timeout=None):
        print self.__name, "recv", "..."
        data = self.in_queue.get(block=True, timeout=timeout)
        print self.__name, "recv", data
        return data
        


def test():
    def f(pipe):
        print "creating debugger"
        qdb = Qdb(pipe=pipe)
        print "set trace"

        my_var = "Mariano!"
        qdb.set_trace()
        print "hello world!"
        print "good by!"
        saraza

    if 'process' in sys.argv:
        from multiprocessing import Process, Pipe
        pipe, child_conn = Pipe()
        p = Process(target=f, args=(child_conn,))

    else:
        from threading import Thread
        from Queue import Queue
        parent_queue, child_queue = Queue(), Queue()
        pipe = QueuePipe("parent", parent_queue, child_queue)
        child_conn = QueuePipe("child", child_queue, parent_queue)
        p = Thread(target=f, args=(child_conn,))
    
    p.start()
    i = 0

    while 1:
        print "<<<", pipe.recv()
        raw_input()
        msg = {'method': 'do_step', 'args': (), 'id': i}
        pipe.send(msg)
        i += 1

    p.join()


class Cli(cmd.Cmd):
    "Qdb Front-end command line interface"
    
    def __init__(self, pipe, completekey='tab', stdin=None, stdout=None, skip=None):
        cmd.Cmd.__init__(self, completekey, stdin, stdout)
        self.i = 0
        self.pipe = pipe

    def attach(self):
        while 1:
            request = self.pipe.recv()
            result = None
            if request.get('method') == 'interaction':
                self.interaction()
                result = None
            if request.get('method') == 'write':
                print request.get("args")[0],
            if request.get('method') == 'debug_event':
                print "%s:%4d\t%s" % request.get("args"),
            if request.get('method') == 'show_line':
                print "%s:%4d%s%s\t%s" % request.get("args"),
            if request.get('method') == 'readline':
                result = raw_input("input...")
            if result:
                response = {'version': '1.1', 'id': request.get('id'), 
                        'result': result, 
                        'error': None}
                self.pipe.send(response)

    def interaction(self):
        self.cmdloop()

    def postcmd(self, stop, line):
        return not line.startswith("h") # stop

    def call(self, method, *args):
        msg = {'method': method, 'args': args, 'id': self.i}
        ##print msg
        self.pipe.send(msg)
        self.i += 1

    do_h = cmd.Cmd.do_help

    def do_s(self, arg):
        "Execute the current line, stop at the first possible occasion"
        self.call('do_step')
        
    def do_n(self, arg):
        "Execute the current line, do not stop at function calls"
        self.call('do_next')

    def do_c(self, arg): 
        "Continue execution, only stop when a breakpoint is encountered."
        self.call('do_continue')
        
    def do_r(self, arg): 
        "Continue execution until the current function returns"
        self.call('do_return')

    def do_j(self, arg): 
        "Set the next line that will be executed."
        self.call('do_jump', arg)

    def do_q(self, arg):
        "Quit from the debugger. The program being executed is aborted."
        self.call('do_quit')
    
    def do_p(self, arg):
        "Inspect the value of the expression"
        self.call('do_inspect', arg)

    def do_l(self, arg):
        "List source code for the current file"
        if arg:
            arg = eval(arg, {}, {})
        self.call('do_list', arg)

    def default(self, line):
        "Default command"
        if line[:1] == '!':
            line = line[1:]
            self.call('do_exec', line)
        else:
            print "*** Unknown command: ", line


def connect(host="localhost", port=6000):
    "Connect to a running debugger backend"
    
    address = (host, port)
    from multiprocessing.connection import Client

    print "waiting for connection to", address
    conn = Client(address, authkey='secret password')
    try:
        Cli(conn).attach()
    finally:
        conn.close()


def main():
    "Debug a script and accept a remote frontend"
    
    if not sys.argv[1:] or sys.argv[1] in ("--help", "-h"):
        print "usage: pdb.py scriptfile [arg] ..."
        sys.exit(2)

    mainpyfile =  sys.argv[1]     # Get script filename
    if not os.path.exists(mainpyfile):
        print 'Error:', mainpyfile, 'does not exist'
        sys.exit(1)

    del sys.argv[0]         # Hide "pdb.py" from argument list

    # Replace pdb's dir with script's dir in front of module search path.
    sys.path[0] = os.path.dirname(mainpyfile)

    from multiprocessing.connection import Listener
    address = ('localhost', 6000)     # family is deduced to be 'AF_INET'
    listener = Listener(address, authkey='secret password')
    print "waiting for connection at", address
    conn = listener.accept()
    print 'connection accepted from', listener.last_accepted

    # create the backend
    qdb = Qdb(conn)
    try:
        print "running", mainpyfile
        qdb._runscript(mainpyfile)
        print "The program finished"
    except SystemExit:
        # In most cases SystemExit does not warrant a post-mortem session.
        print "The program exited via sys.exit(). Exit status: ",
        print sys.exc_info()[1]
    except:
        traceback.print_exc()
        print "Uncaught exception. Entering post mortem debugging"
        t = sys.exc_info()[2]
        qdb.post_mortem(t)

    conn.close()
    listener.close()


if __name__ == '__main__':
    # When invoked as main program:
    #test()
    if not sys.argv[1:]:
        # connect to a remote debbuger
        connect()
    else:
        # start the debugger on a script
        # reimport as global __main__ namespace is destroyed
        import qdb
        qdb.main()

