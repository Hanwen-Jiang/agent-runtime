"""One JSON request on stdin, one JSON response on stdout; logs go to stderr."""
from __future__ import annotations
import contextlib
import json
import os
import sys
from .models import Context, FileEngineError, canonical


def process(request: dict) -> dict:
    ctx = None
    try:
        operation = str(request.get('operation', 'inspect'))
        if operation in ('table.recalculate', 'recalculate'):
            from .recalculate import process_recalculate
            return process_recalculate(request)
        if operation.startswith('table.'):
            from .compute import process_compute
            return process_compute(request)
        if operation.startswith(('document.', 'notebook.')):
            from .edit import process_edit
            return process_edit(request)
        ctx = Context.from_request(request)
        if operation in ('inspect','file.inspect'):
            from .inspect import inspect_file
            response = inspect_file(ctx)
        elif operation in ('read','file.read'):
            from .parse import read
            response = read(ctx)
        elif operation in ('render','file.render','file.open_region'):
            from .render import render
            response = render(ctx)
        elif operation in ('archive_list','extract','archive.list','archive.extract'):
            from .parse import archive
            response = archive(ctx)
        else:
            raise FileEngineError('unsupported_operation','operation is not supported by this engine','unsupported')
        ir = ctx.write_bytes('representation.json', canonical(response))
        response['artifacts'].append(ctx.artifact(ir, 'application/json', {'kind':'representation'}))
        return response
    except FileEngineError as error:
        response = {'status':error.status,'error':{'code':error.code,'message':error.message},'blocks':[],'outline':[],'warnings':[],'coverage':{'complete':False},'artifacts':[],'capabilities':{}}
        if ctx is not None:
            response.update({key: value for key, value in ctx.response(status=error.status).items() if key in ('source','processor')})
        return response
    except (ImportError, ModuleNotFoundError) as error:
        return {'status':'unsupported','error':{'code':'engine_dependency_missing','message':str(error)},'blocks':[],'artifacts':[],'coverage':{'complete':False},'warnings':[],'outline':[],'capabilities':{}}
    except Exception as error:
        return {'status':'failed','error':{'code':'file_processing_failed','message':type(error).__name__},'blocks':[],'artifacts':[],'coverage':{'complete':False},'warnings':[],'outline':[],'capabilities':{}}


def main():
    raw = sys.stdin.buffer.read((1 << 20) + 1)
    if len(raw)>1<<20:
        response={'status':'failed','error':{'code':'request_too_large','message':'request exceeds 1 MiB'}}
    else:
        try:
            request=json.loads(raw)
            # A sandbox has deny-by-default networking. Libraries also must not
            # download models behind the application's capability declaration.
            os.environ.setdefault('HF_HUB_OFFLINE','1')
            os.environ.setdefault('TRANSFORMERS_OFFLINE','1')
            with contextlib.redirect_stdout(sys.stderr):
                response=process(request)
        except Exception as error:
            response={'status':'failed','error':{'code':'invalid_request','message':type(error).__name__}}
    sys.stdout.buffer.write(canonical(response)+b'\n')


if __name__=='__main__':
    main()
