# ANSI/NISO Z39.86-2005 document type definitions

These are the published DTDs of the DAISY 3 standard (ANSI/NISO Z39.86-2005),
used by `app/daisy_dtd.py` to validate every generated package offline.

The original download location (`http://www.daisy.org/z3986/2005/`) no longer
serves them, so they were taken unmodified from the DAISY Consortium's own
repository on 2026-10-25:

    https://github.com/daisy/urakawa-sdk/tree/master/csharp/dtds/Resources

| File | Document |
|---|---|
| `dtbook-2005-3.dtd` | DTBook text |
| `dtbsmil-2005-2.dtd` | SMIL synchronization |
| `ncx-2005-1.dtd` | NCX navigation |
| `resource-2005-1.dtd` | resource file |
| `oebpkg12.dtd`, `oeb12.ent` | OEB 1.2 package file (OPF) |

Do not edit them: a validation against a modified DTD proves nothing.
