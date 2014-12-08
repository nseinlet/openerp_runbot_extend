from openerp.osv import fields, osv
from openerp.addons.runbot import runbot
import psutil
import os
import signal

loglevels = (('none', 'None'),
             ('warning', 'Warning'),
             ('error', 'Error'))

#_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
#_re_error = r'^.*FAILED.*$'
#_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '

class runbot_repo(osv.Model):
    _inherit = "runbot.repo"

    _columns = {
        'db_name': fields.char("Database name to replicate"),
        'nobuild': fields.boolean('Do not buid'),
        'sequence': fields.integer('Sequence of display', select=True),
        'error': fields.selection(loglevels, 'Error messages'),
        'critical': fields.selection(loglevels, 'Critical messages'),
        'traceback': fields.selection(loglevels, 'Traceback messages'),
        'warning': fields.selection(loglevels, 'Warning messages'),
        'failed': fields.selection(loglevels, 'Failed messages'),
    }

    _defaults = {
        'error': 'error',
        'critical': 'error',
        'traceback': 'error',
        'warning': 'warning',
        'failed': 'none',
    }

    _order = 'sequence'

    def update_git(self, cr, uid, repo, context=None):
        super(runbot_repo, self).update_git(cr, uid, repo, context)
        if repo.nobuild:
            bds = self.pool['runbot.build']
            bds_ids = bds.search(cr, uid, [('repo_id', '=', repo.id), ('state', '=', 'pending')], context=context)
            bds.write(cr, uid, bds_ids, {'state': 'done'}, context=context)

class runbot_build(osv.osv):
    _inherit = "runbot.build"

    def job_25_restore(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name:
            return 0
        self.pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd = "pg_dump %s | psql %s-all" % (build.repo_id.db_name, build.dest)
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)

    def job_26_upgrade(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name:
            return 0
        cmd, mods = build.cmd()
        cmd += ['-d', '%s-all' % build.dest, '-u', 'all', '--stop-after-init', '--log-level=debug', '--test-enable']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def job_30_run(self, cr, uid, build, lock_path, log_path):
        runbot._re_error = self._get_regexeforlog(build=build, errlevel='error')
        runbot._re_warning = self._get_regexeforlog(build=build, errlevel='warning')
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        log_all = build.path('logs', 'job_20_test_all.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time),
        }
        if grep(log_all, ".modules.loading: Modules loaded."):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif not grep(build.server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                v['result'] = "ok"
        else:
            v['result'] = "ko"
        build.write(v)
        build.github_status()

        # run server
        cmd, mods = build.cmd()
        if os.path.exists(build.server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % build.dest]

        if grep(build.server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter','%d.*$']
            else:
                cmd += ['--db-filter','%s.*$' % build.dest]	
	cmd.append('--stop-after-init')
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None, showstderr=True)


    def job_31_run(self, cr, uid, build, lock_path, log_path):
        if build.result == "ko":
	    return True
	runbot._re_error = self._get_regexeforlog(build=build, errlevel='error')
        runbot._re_warning = self._get_regexeforlog(build=build, errlevel='warning')
        build._log('run', 'Start running build %s' % build.dest)
        log_all = build.path('logs', 'job_26_upgrade.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time),
        }
        if grep(log_all, ".modules.loading: Modules loaded."):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif not grep(build.server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                v['result'] = "ok"
        else:
            v['result'] = "ko"
        build.write(v)
        build.github_status()

        # run server
        cmd, mods = build.cmd()
        if os.path.exists(build.server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % build.dest]

        if grep(build.server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter','%d.*$']
            else:
                cmd += ['--db-filter','%s.*$' % build.dest]

	return self.spawn(cmd, lock_path, log_path, cpu_limit=None, showstderr=True)

    def get_closest_branch_name(self, cr, uid, ids, target_repo_id, hint_branches, context=None):
        """Return the name of the odoo branch
        """
        for build in self.browse(cr, uid, ids, context=context):
            name = build.branch_id.branch_name
            if name.split('-',1)[0] == "saas":
                name = "%s-%s" % (name.split('-',1)[0], name.split('-',2)[1])
            else:
                name = name.split('-',1)[0]
            return name

    def _get_regexeforlog(self, build, errlevel):
        addederror = False
        regex = r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ '
        if build.repo_id.error == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(ERROR)"
        if build.repo_id.critical == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(CRITICAL)"
        if build.repo_id.warning == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(WARNING)"
        if build.repo_id.failed == errlevel:
            if addederror:
                regex += "|"
            else:
                addederror = True
            regex +="(TEST.*FAIL)"
        if build.repo_id.traceback == errlevel:
            if addederror:
                regex = '(Traceback \(most recent call last\))|(%s)' % regex
            else:
                regex = '(Traceback \(most recent call last\))'
        #regex = '^' + regex + '$'
        return regex

    def job_21_checkdeadbuild(self, cr, uid, build, lock_path, log_path):
        for proc in psutil.process_iter():
            if proc.name() in ('openerp', 'python', 'openerp-server'):
                lgn = proc.cmdline()
                if ('--xmlrpc-port=%s' % build.port) in lgn:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
