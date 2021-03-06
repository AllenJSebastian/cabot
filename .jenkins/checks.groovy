/**
 * checks.groovy
 * Generates projects for running tests for cabot.
 */

String script(target) {
  """\
  |#!/bin/bash
  |set -o nounset
  |set -o errexit
  |set -o pipefail
  |rm -rf .tox # rm old virtualenvs
  |PY_COLORS=1 tox -e ${target}
  """.stripMargin()
}

Closure jobTemplate(job, desc, target) {

  def closure = {
    description("""\
      |<p>${desc}</p>
      |<p>
      |  This project was generated using <a href='/job/cabot.generator/'>cabot.generator</a>.
      |  Manual changes <em>will be discarded</em>.
      |</p>
      |<h4><a href='lastBuild/console'>View console output for latest build.</a></h4>
    """.stripMargin())

    parameters {
      stringParam('GIT_BRANCH', 'master', '<p><em>e.g.</em> <code>master</code></p>')
      stringParam('GIT_COMMIT', 'develop', '<p>git commit-ish, usually provided by maestro</p>')
    }

    logRotator {
      daysToKeep(5)
    }

    label '"docker1.8"'

    scm {
      git {
        remote {
          github('Affirm/cabot', 'ssh')
          credentials('github-ci')
        }
        clean()
        pruneBranches(true)
        branch('${GIT_COMMIT}')
        localBranch('${GIT_BRANCH}')
        reference('/mnt/jenkins/git/cabot')
      }
    }

    concurrentBuild(true)

    wrappers {
      colorizeOutput()
      timestamps()
      timeout {
        failBuild()
        noActivity(360)
      }
      buildName('#${BUILD_NUMBER} - ${GIT_REVISION,length=10}@${ENV,var="GIT_BRANCH"}')
      maskPasswords()
    }

    steps {
      shell(script(target))
    }
  }

  closure.delegate = job
  return closure
}

// generate project for tests using postgres
freeStyleJob('cabot.test-psql') { job ->
  jobTemplate(job, "Runs tests against Affirm/cabot.git with a Postgres database", 'test-psql').call()
  publishers {
    archiveJunit('build/test-psql/*.xml')
    postBuildScripts {
      steps {
        shell('sudo chown -R "$(whoami)" "${WORKSPACE}"')
      }
      onlyIfBuildSucceeds(false)
    }
  }
}

// generate project for tests using mysql
freeStyleJob('cabot.test-mysql') { job ->
  jobTemplate(job, "Runs tests against Affirm/cabot.git with a MySQL database", 'test-mysql').call()
  publishers {
    archiveJunit('build/test-mysql/*.xml')
    postBuildScripts {
      steps {
        shell('sudo chown -R "$(whoami)" "${WORKSPACE}"')
      }
      onlyIfBuildSucceeds(false)
    }
  }
}

// generate projects for flake8 tests
freeStyleJob('cabot.flake8') { job ->
  jobTemplate(job, "Runs tests against Affirm/cabot.git using flake8", 'flake8').call()
  publishers {
    violations {
      pep8(1, 1, 1, 'build/flake8/flake8.txt')
    }
  }
}
