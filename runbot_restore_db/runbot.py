import psutil
import datetime
import os
import re
import signal
import time
import glob
import shutil
import subprocess
import resource

import openerp
from openerp.osv import fields, osv
from openerp.addons.runbot import runbot
from openerp.addons.runbot.runbot import log, dashes, mkdirs, grep, rfind, lock, locked, nowait, run, now, dt2time, s2human, flatten, decode_utf, uniq_list, fqdn
from openerp.addons.runbot.runbot import _re_error, _re_warning, _re_job, _logger


loglevels = (('none', 'None'),
             ('warning', 'Warning'),
             ('error', 'Error'))

class runbot_build(osv.osv):
    _inherit = "runbot.build"

    def spawncwd(self, cmd, lock_path, log_path, cpu_limit=None, shell=False, cwd=None):
        def preexec_fn():
            os.setsid()
            if cpu_limit:
                # set soft cpulimit
                soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
                r = resource.getrusage(resource.RUSAGE_SELF)
                cpu_time = r.ru_utime + r.ru_stime
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_time + cpu_limit, hard))
            # close parent files
            os.closerange(3, os.sysconf("SC_OPEN_MAX"))
            lock(lock_path)
        out=open(log_path,"w")
        _logger.info("spawn: %s in %s stdout: %s", ' '.join(cmd), str(cwd), log_path)
        p=subprocess.Popen(cmd, stdout=out, stderr=out, preexec_fn=preexec_fn, shell=shell, cwd=cwd)
        return p.pid

    def checkout(self, cr, uid, ids, context=None):
        super(runbot_build, self).checkout(cr, uid, ids, context)

        #Check uploadable adon (EDI server)
        for build in self.browse(cr, uid, ids, context=context):
            # move all addons to server addons path
            for module in set(glob.glob(build.path('uploadable addon/*'))):
                basename = os.path.basename(module)
                if not os.path.exists(build.server('addons', basename)):
                    shutil.move(module, build.server('addons'))
                else:
                    build._log(
                        'Building environment',
                        'You have duplicate modules in your branches "%s"' % basename
                    )

    def job_20_test_all(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.docoverage:
            build._log('test_all', 'Start test all modules')
            path = self.path(cr, uid, build.id)
            self.pg_createdb(cr, uid, "%s-all" % build.dest)
            cmd, mods = build.cmd()
            if grep(build.server("tools/config.py"), "test-enable"):
                cmd.append("--test-enable")
            cmd += ['-d', '%s-all' % build.dest, '-i', mods, '--stop-after-init', '--log-level=test', '--max-cron-threads=0']
            cmd = ['coverage', 'run', '--include=openerp/addons/*'] + cmd[1:]
            # reset job_start to an accurate job_20 job_time
            build.write({'job_start': now()})
            build._log('job_20_coverage', " ".join(cmd))
            return self.spawncwd(cmd, lock_path, log_path, cpu_limit=3500, cwd=path)
        else:
            return super(runbot_build, self).job_20_test_all(cr, uid, build, lock_path, log_path)

    def job_21_checkdeadbuild(self, cr, uid, build, lock_path, log_path):
        for proc in psutil.process_iter():
            if proc.name in ('openerp', 'python', 'openerp-server'):
                lgn = proc.cmdline
                if ('--xmlrpc-port=%s' % build.port) in lgn:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass

    def job_22_coverage_report(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.docoverage:
            path = self.path(cr, uid, build.id)
            cmd, mods = build.cmd()
            if mods:
                include = ",".join(["openerp/addons/%s" % mod.replace(" ","") for mod in mods.split(",")])
            else:
                include = "openerp/addons/*"
            cmd = ['coverage', 'report' ,'--include=%s' % include]
            build._log('coverage_report', " ".join(cmd))
            return self.spawncwd(cmd, lock_path, log_path, cpu_limit=None, cwd=path)
        else:
            return 0

    def job_23_coverage_report_html(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.docoverage:
            path = self.path(cr, uid, build.id)
            cmd, mods = build.cmd()
            if mods:
                include = ",".join(["openerp/addons/%s" % mod for mod in mods])
            else:
                include = "openerp/addons/*"
            cmd = ['coverage', 'html', '--include=%s' % include, "-d", "logs/coverage"]
            return self.spawncwd(cmd, lock_path, log_path, cpu_limit=None, cwd=path)
        else:
            return 0

    def job_25_restore(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name:
            return 0
        self.pg_createdb(cr, uid, "%s-all" % build.dest)
        cmd = "pg_dump %s | psql %s-all" % (build.repo_id.db_name, build.dest)
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)

    def job_26_upgrade(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name:
            return 0
        to_test = build.repo_id.modules if build.repo_id.modules else 'all'
        cmd, mods = build.cmd()
        cmd += ['-d', '%s-all' % build.dest, '-u', to_test, '--stop-after-init', '--log-level=debug']
        if not build.repo_id.no_testenable_job26:
            cmd.append("--test-enable")
        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def job_30_run(self, cr, uid, build, lock_path, log_path):
        if build.repo_id.db_name and build.state == 'running' and build.result == "ko":
            return 0
        runbot._re_error = self._get_regexeforlog(build=build, errlevel='error')
        runbot._re_warning = self._get_regexeforlog(build=build, errlevel='warning')

        build._log('run', 'Start running build %s' % build.dest)

        v = {}
        result = "ok"
        log_names = [elmt.name for elmt in build.repo_id.parse_job_ids]
        for log_name in log_names:
            log_all = build.path('logs', log_name+'.txt')
            if grep(log_all, ".modules.loading: Modules loaded."):
                if rfind(log_all, runbot._re_error):
                    result = "ko"
                    break;
                elif rfind(log_all, runbot._re_warning):
                    result = "warn"
                elif not grep(build.server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                    if result != "warn":
                        result = "ok"
            else:
                result = "ko"
                break;
            log_time = time.localtime(os.path.getmtime(log_all))
            v['job_end'] = time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time)
        v['result'] = result
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

        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    def get_closest_branch_name(self, cr, uid, ids, target_repo_id, hint_branches, context=None):
        """Return the name of the odoo branch
        """
        for build in self.browse(cr, uid, ids, context=context):
            name = build.branch_id.branch_name
            if name.split('-',1)[0] == "saas":
                name = "%s-%s" % (name.split('-',1)[0], name.split('-',2)[1])
            else:
                name = name.split('-',1)[0]
            #retrieve last commit id for this branch
            build_ids = self.search(cr, uid, [('repo_id', '=', target_repo_id), ('branch_id.branch_name', '=', name)])
            if build_ids:
                thebuild = self.browse(cr, uid, build_ids, context=context)
                if thebuild:
                    return thebuild[0].name
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

    def schedule(self, cr, uid, ids, context=None):
        """
        /!\ must rewrite the all method because for each build we need
            to remove jobs that were specified as skipped in the repo.
        """
        all_jobs = self.list_jobs()
        icp = self.pool['ir.config_parameter']
        timeout = int(icp.get_param(cr, uid, 'runbot.timeout', default=1800))

        for build in self.browse(cr, uid, ids, context=context):
            #remove skipped jobs
            jobs = all_jobs[:]
            for job_to_skip in build.repo_id.skip_job_ids:
                jobs.remove(job_to_skip.name)
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self.find_port(cr, uid)
                values = {
                    'host': fqdn(),
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                }
                build.write(values)
                cr.commit()
            else:
                # check if current job is finished
                lock_path = build.path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build.logger('%s time exceded (%ss)', build.job, build.job_time)
                        build.kill(result='killed')
                    continue
                build.logger('%s finished', build.job)
                # schedule
                v = {}
                # testing -> running
                if build.job == jobs[-2]:
                    v['state'] = 'running'
                    v['job'] = jobs[-1]
                    v['job_end'] = now(),
                # running -> done
                elif build.job == jobs[-1]:
                    v['state'] = 'done'
                    v['job'] = ''
                # testing
                else:
                    v['job'] = jobs[jobs.index(build.job) + 1]
                build.write(v)
            build.refresh()

            # run job
            pid = None
            if build.state != 'done':
                build.logger('running %s', build.job)
                job_method = getattr(self,build.job)
                lock_path = build.path('logs', '%s.lock' % build.job)
                log_path = build.path('logs', '%s.txt' % build.job)
                pid = job_method(cr, uid, build, lock_path, log_path)
                build.write({'pid': pid})
            # needed to prevent losing pids if multiple jobs are started and one them raise an exception
            cr.commit()

            if pid == -2:
                # no process to wait, directly call next job
                # FIXME find a better way that this recursive call
                build.schedule()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build.cleanup()

class job(osv.Model):
    _name = "runbot.job"

    _columns = {
        'name': fields.char("Job name")
    }

class runbot_repo(osv.Model):
    _inherit = "runbot.repo"

    def cron_update_job(self, cr, uid, context=None):
        build_obj = self.pool.get('runbot.build')
        jobs = build_obj.list_jobs()
        job_obj = self.pool.get('runbot.job')
        for job_name in jobs:
            job_id = job_obj.search(cr, uid, [('name', '=', job_name)])
            if not job_id:
                job_obj.create(cr, uid, {'name': job_name})
        job_to_rm_ids = job_obj.search(cr, 1, [('name', 'not in', jobs)])
        job_obj.unlink(cr, uid, job_to_rm_ids)
        return True

    _columns = {
        'db_name': fields.char("Database name to replicate"),
        'nobuild': fields.boolean('Do not build'),
        'docoverage': fields.boolean('Do coverage testing'),
        'sequence': fields.integer('Sequence of display', select=True),
        'error': fields.selection(loglevels, 'Error messages'),
        'critical': fields.selection(loglevels, 'Critical messages'),
        'traceback': fields.selection(loglevels, 'Traceback messages'),
        'warning': fields.selection(loglevels, 'Warning messages'),
        'failed': fields.selection(loglevels, 'Failed messages'),
        'skip_job_ids': fields.many2many('runbot.job', string='Jobs to skip'),
        'parse_job_ids': fields.many2many('runbot.job', "repo_parse_job_rel", string='Jobs to parse'),
        'no_testenable_job26': fields.boolean('No test-enabled', help='No test-enabled on job 26 (test-enable is unknown for 6.1)'),
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
            bds_ids = bds.search(cr, uid, [('repo_id', '=', repo.id), ('state', '=', 'pending'), ('branch_id.sticky', '=', False)], context=context)
            bds.write(cr, uid, bds_ids, {'state': 'done'}, context=context)

class RunbotControllerPS(runbot.RunbotController):

    def build_info(self, build):
        res = super(RunbotControllerPS, self).build_info(build)
        res['parse_job_ids'] = [elmt.name for elmt in build.repo_id.parse_job_ids]
        res['docoverage'] = build.repo_id.docoverage
        return res
