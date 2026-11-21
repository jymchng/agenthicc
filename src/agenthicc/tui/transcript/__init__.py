"""The transcript view, search, fullscreen pane and ``/rewind`` (TUX-09).

Nothing here is imported by the first frame: the commands and key actions that
open the view import it when they run. The modules:

- :mod:`~agenthicc.tui.transcript.text`: an event's plain text and the search
  matching rule;
- :mod:`~agenthicc.tui.transcript.render`: an event as rows, drawn by the
  scrollback's own renderers (TUX-06);
- :mod:`~agenthicc.tui.transcript.log`: the offset index, search and windows of
  a log file;
- :mod:`~agenthicc.tui.transcript.sources`: what a view shows (the session log,
  a text file, a document);
- :mod:`~agenthicc.tui.transcript.view`: selection, scrolling, keys and chrome;
- :mod:`~agenthicc.tui.transcript.host`: the alternate-screen loop;
- :mod:`~agenthicc.tui.transcript.clipboard`: OSC 52 and the system clipboard;
- :mod:`~agenthicc.tui.transcript.fullscreen`: the docked-composer layout;
- :mod:`~agenthicc.tui.transcript.fork`: ``/rewind``'s session fork.
"""
