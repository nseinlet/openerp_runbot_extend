import psutil
import datetime
import os
import re
import signal
import time
import glob
import shutil

import openerp
from openerp.osv import fields, osv
from openerp.addons.runbot import runbot
from openerp.addons.runbot.runbot import log, dashes, mkdirs, grep, rfind, lock, locked, nowait, run, now, dt2time, s2human, flatten, decode_utf, uniq_list, fqdn, local_pgadmin_cursor
from openerp.addons.runbot.runbot import _re_error, _re_warning, _re_job, _logger


loglevels = (('none', 'None'),
             ('warning', 'Warning'),
             ('error', 'Error'))

class RunbotBranch(osv.osv):
    _inherit = "runbot.branch"
    
    _columns = {
        'db_name': fields.char("Database name to replicate"),
    }
    
    def _get_branch_quickconnect_url(self, cr, uid, ids, fqdn, dest, context=None):
        r = {}
        for branch in self.browse(cr, uid, ids, context=context):
            if branch.branch_name.startswith('7'):
                r[branch.id] = "http://%s/login?db=%s-%s&login=admin&key=admin" % (fqdn, dest, 'custom' if branch.repo_id.db_name or branch.db_name else 'all')
            elif branch.name.startswith('8'):
                r[branch.id] = "http://%s/login?db=%s-%s&login=admin&key=admin&redirect=/web?debug=1" % (fqdn, dest, 'custom' if branch.repo_id.db_name or branch.db_name else 'all')
            else:
                r[branch.id] = "http://%s/web/login?db=%s-%s&login=admin&redirect=/web?debug=1" % (fqdn, dest, 'custom' if branch.repo_id.db_name or branch.db_name else 'all')
        return r

class RunbotBuild(osv.osv):
    _inherit = "runbot.build"

    def _local_pg_dropdb(self, cr, uid, dbname):
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity  WHERE datname = '%s' and pid != pg_backend_pid()" % dbname)
        super(RunbotBuild, self)._local_pg_dropdb(cr, uid, dbname)

    def _checkout(self, cr, uid, ids, context=None):
        super(RunbotBuild, self)._checkout(cr, uid, ids, context)
        reps = ('uploadable addon', 'trial', 'default')

        #Check uploadable adon (EDI server)
        for build in self.browse(cr, uid, ids, context=context):
            # move all addons to server addons path
            modules_to_test = build.modules.split(",")
            for rep in reps:
                for module in set(glob.glob(build._path('%s/*' % rep))):
                    basename = os.path.basename(module)
                    if not os.path.exists(build._server('addons', basename)):
                        shutil.move(module, build._server('addons'))
                        if basename[:5]!="saas_":
                            modules_to_test.append(basename)
                    else:
                        build._log(
                            'Building environment',
                            'You have duplicate modules in your branches "%s"' % basename
                        )
            build.write({'modules': ','.join(modules_to_test)})

    def _job_25_restore(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name and not build.branch_id.db_name:
            return 0
        self._local_pg_createdb(cr, uid, "%s-custom" % build.dest)
        cmd = "pg_dump %s | psql %s-custom" % (build.branch_id.db_name or build.repo_id.db_name, build.dest)
        return self._spawn(cmd, lock_path, log_path, cpu_limit=None, shell=True)

    def _job_26_upgrade(self, cr, uid, build, lock_path, log_path):
        if not build.repo_id.db_name and not build.branch_id.db_name:
            return 0
        to_test = build.modules if build.modules and not build.repo_id.force_update_all else 'all'
        cmd, mods = build._cmd()
        cmd += ['-d', '%s-custom' % build.dest, '-u', to_test, '--stop-after-init', '--log-level=info']
        if not build.repo_id.no_testenable_job26:
            cmd.append("--test-enable")
        return self._spawn(cmd, lock_path, log_path, cpu_limit=None)

    def _job_30_run(self, cr, uid, build, lock_path, log_path):
        if (build.repo_id.db_name or build.branch_id.db_name) and build.state == 'running' and build.result == "ko":
            return 0
        runbot._re_error = self._get_regexeforlog(build=build, errlevel='error')
        runbot._re_warning = self._get_regexeforlog(build=build, errlevel='warning')

        build._log('run', 'Start running build %s' % build.dest)

        v = {}
        result = "ok"
        log_names = [elmt.name for elmt in build.repo_id.parse_job_ids]
        for log_name in log_names:
            log_all = build._path('logs', log_name+'.txt')
            if grep(log_all, ".modules.loading: Modules loaded."):
                if rfind(log_all, runbot._re_error):
                    result = "ko"
                    break
                elif rfind(log_all, runbot._re_warning):
                    result = "warn"
                elif not grep(build._server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                    if result != "warn":
                        result = "ok"
            else:
                result = "ko"
                break
            log_time = time.localtime(os.path.getmtime(log_all))
            v['job_end'] = time.strftime(openerp.tools.DEFAULT_SERVER_DATETIME_FORMAT, log_time)
        v['result'] = result
        build.write(v)
        build._github_status()

        # run server
        cmd, mods = build._cmd()
        if os.path.exists(build._server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % build.dest]

        if grep(build._server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter','%d.*']
            else:
                cmd += ['--db-filter','%s.*$' % build.dest]

        return self._spawn(cmd, lock_path, log_path, cpu_limit=None)

    def _get_closest_branch_name(self, cr, uid, ids, target_repo_id, context=None):
        """Return the name of the odoo branch
        """
        result_for = lambda d: (d.repo_id.id, d.name, 'exact')

        for build in self.browse(cr, uid, ids, context=context):
            branch = build.branch_id
            pi = branch._get_pull_info()
            name = pi['base']['ref'] if pi else branch.branch_name
            if name.split('-',1)[0] == "saas":
                name = "%s-%s" % (name.split('-',1)[0], name.split('-',2)[1])
            else:
                name = name.split('-',1)[0]
            #Check replacing names
            for forced_branch in build.repo_id.forced_branch_ids:
                if forced_branch.name == name and forced_branch.dep_repo_id.id == target_repo_id:
                    name = forced_branch.forced_name
                    break
            #retrieve last commit id for this branch
            build_ids = self.search(cr, uid, [('repo_id', '=', target_repo_id), ('branch_id.branch_name', '=', name)])
            if build_ids:
                thebuild = self.browse(cr, uid, build_ids, context=context)
                if thebuild:
                    return result_for(thebuild[0].branch_id)
            return target_repo_id, name, 'default'

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

    def _schedule(self, cr, uid, ids, context=None):
        all_jobs = self._list_jobs()
        icp = self.pool['ir.config_parameter']
        timeout = int(icp.get_param(cr, uid, 'runbot.timeout', default=1800))

        for build in self.browse(cr, uid, ids, context=context):
            #remove skipped jobs
            jobs = all_jobs[:]
            for job_to_skip in build.repo_id.skip_job_ids:
                jobs.remove(job_to_skip.name)
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self._find_port(cr, uid)
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
                lock_path = build._path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build._logger('%s time exceded (%ss)', build.job, build.job_time)
                        build.write({'job_end': now()})
                        build._kill(result='killed')
                    continue
                build._logger('%s finished', build.job)
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
                build._logger('running %s', build.job)
                job_method = getattr(self, '_' + build.job)
                mkdirs([build._path('logs')])
                lock_path = build._path('logs', '%s.lock' % build.job)
                log_path = build._path('logs', '%s.txt' % build.job)
                try:
                    pid = job_method(cr, uid, build, lock_path, log_path)
                    build.write({'pid': pid})
                except Exception:
                    _logger.exception('%s failed running method %s', build.dest, build.job)
                    build._log(build.job, "failed running job method, see runbot log")
                    build._kill(result='ko')
                    continue
            # needed to prevent losing pids if multiple jobs are started and one them raise an exception
            cr.commit()

            if pid == -2:
                # no process to wait, directly call next job
                # FIXME find a better way that this recursive call
                build._schedule()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build._local_cleanup()

    def _cmd(self, cr, uid, ids, context=None):
        """Return a list describing the command to start the build"""
        cmd, modules = super(RunbotBuild, self)._cmd(cr, uid, ids, context)
        for build in self.browse(cr, uid, ids, context=context):
            if build.repo_id.custom_config:
                rbc = self.pool.get('runbot.build.configuration').create(cr, uid, {
                    'name': 'custom config build %s' % build.id,
                    'model': 'runbot.build',
                    'type': 'qweb',
                    'arch': "<?xml version='1.0'?><t t-name='runbot.build_config_%s'>%s</t>" % (build.id, build.repo_id.custom_config),
                }, context=context)
                settings = {'build': build}
                build_config = self.pool['runbot.build.configuration'].render(cr, uid, rbc, settings)
                with open("%s/build.cfg" % build._path(), 'w+') as cfg:
                    cfg.write("[options]\n")
                    cfg.write(build_config)
                cmd += ["-c", "%s/build.cfg" % build._path()]
        return cmd, modules
            
            
class job(osv.Model):
    _name = "runbot.job"

    _columns = {
        'name': fields.char("Job name")
    }


class runbot_repo(osv.Model):
    _inherit = "runbot.repo"

    def cron_update_job(self, cr, uid, context=None):
        build_obj = self.pool.get('runbot.build')
        jobs = build_obj._list_jobs()
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
        'force_update_all' : fields.boolean("Force Update ALL", help="Force update all on job_26 otherwise it will update only the modules in the repository"),
        'nobuild': fields.boolean('Do not build'),
        'error': fields.selection(loglevels, 'Error messages'),
        'critical': fields.selection(loglevels, 'Critical messages'),
        'traceback': fields.selection(loglevels, 'Traceback messages'),
        'warning': fields.selection(loglevels, 'Warning messages'),
        'failed': fields.selection(loglevels, 'Failed messages'),
        'skip_job_ids': fields.many2many('runbot.job', string='Jobs to skip'),
        'parse_job_ids': fields.many2many('runbot.job', "repo_parse_job_rel", string='Jobs to parse'),
        'no_testenable_job26': fields.boolean('No test-enabled', help='No test-enabled on job 26 (test-enable is unknown for 6.1)'),
        'forced_branch_ids': fields.one2many('runbot.forced.branch', 'repo_id', string='Replacing branch names'),
        'custom_config': fields.text('Custom configuration', help="This config will be placed in a text file, behind the [option] line, and passed with a -c to the jobs.")
    }

    _defaults = {
        'error': 'error',
        'critical': 'error',
        'traceback': 'error',
        'warning': 'warning',
        'failed': 'none',
    }

    def _update_git(self, cr, uid, repo, context=None):
        super(runbot_repo, self)._update_git(cr, uid, repo, context)
        if repo.nobuild:
            bds = self.pool['runbot.build']
            bds_ids = bds.search(cr, uid, [('repo_id', '=', repo.id), ('state', '=', 'pending'), ('branch_id.sticky', '=', False)], context=context)
            bds.write(cr, uid, bds_ids, {'state': 'done'}, context=context)


class runbot_forced_branch(osv.Model):
    _name = "runbot.forced.branch"

    _columns = {
        'repo_id': fields.many2one('runbot.repo', 'Repository', required=True, ondelete='cascade', select=1),
        'dep_repo_id': fields.many2one('runbot.repo', required=True, string="For dep. repo"),
        'name': fields.char('Branch name to replace', required=True),
        'forced_name': fields.char('Replacing branch name', required=True),
    }


class RunbotControllerPS(runbot.RunbotController):

    def build_info(self, build):
        res = super(RunbotControllerPS, self).build_info(build)
        res['parse_job_ids'] = [elmt.name for elmt in build.repo_id.parse_job_ids]
        res['restored_db_name'] = build.branch_id.db_name or build.repo_id.db_name
        return res


class BuildConfig(osv.osv_memory):
    _name = 'runbot.build.configuration'
    _description = 'Runbot build custom configuration'
    _inherit = 'ir.ui.view'
    
    
