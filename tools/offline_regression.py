"""Offline regression: never launch the operating GUI/runner or use real credentials."""
import argparse,ast,contextlib,json,os,socket,sys,unittest
from pathlib import Path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dependencies',type=Path,required=True);parser.add_argument('--test-qt',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve();deps=args.dependencies.resolve();extra=args.test_qt.resolve()
    if (root/'runtime').exists() or (root/'config.json').exists() or (root/'automation.json').exists():
        raise RuntimeError('Regression must use a source-only development/export tree, not operating state')
    sys.dont_write_bytecode=True;os.environ['PYTHONDONTWRITEBYTECODE']='1';os.environ['QT_QPA_PLATFORM']='offscreen'
    sys.path[:0]=[str(root),str(root/'tests'),str(deps/'vendor'),str(deps/'gui_vendor')]
    network=[]
    def denied(*a,**k):network.append('blocked');raise AssertionError('Network forbidden in offline regression')
    socket.socket.connect=denied;socket.create_connection=denied
    import PySide6
    PySide6.__path__.insert(0,str(extra/'PySide6'))
    handles=[]
    if os.name=='nt':handles.append(os.add_dll_directory(str(extra/'PySide6')))
    files=sorted(list(root.glob('*.py'))+list((root/'core').glob('*.py'))+list((root/'automation').glob('*.py'))+list((root/'tests').glob('test_*.py'))+list((root/'tools').glob('*.py')))
    syntax=[]
    for p in files:
        try:ast.parse(p.read_text(encoding='utf-8-sig'),filename=str(p))
        except SyntaxError as e:syntax.append({'file':p.relative_to(root).as_posix(),'error':str(e)})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.with_suffix('.log').open('w',encoding='utf-8') as stream,contextlib.redirect_stdout(stream),contextlib.redirect_stderr(stream):
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.discover(str(root/'tests')))
    report={'tests':result.testsRun,'PASS':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        'FAIL':len(result.failures),'ERROR':len(result.errors),'SKIP':len(result.skipped),'syntax_files':len(files),'syntax_errors':syntax,
        'network_attempts':len(network),'actual_orders':0,'actual_program_started':False,
        'production_dependencies':str(deps),'test_only_qt_overlay':str(extra)}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    return 0 if result.wasSuccessful() and not syntax and not network else 1

if __name__=='__main__':raise SystemExit(main())
