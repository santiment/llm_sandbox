@Library('podTemplateLib')

import net.santiment.utils.podTemplates

properties([
  buildDiscarder(
    logRotator(
      artifactDaysToKeepStr: '30',
      artifactNumToKeepStr: '',
      daysToKeepStr: '30',
      numToKeepStr: ''
    )
  )
])

slaveTemplates = new podTemplates()

slaveTemplates.dockerTemplate { label ->
  node(label) {
    container('docker') {
      def scmVars = checkout scm

      // Every branch and PR: build the test image (Dockerfile `test` target = build stage
      // + dev deps + tests/) and run the suite. The tests use local fakes only — no docker
      // daemon, no apiserver — so a plain `docker run` is enough.
      stage('Run tests') {
        def testImage = "llm-sandbox:test-${scmVars.GIT_COMMIT}-${env.BUILD_ID}"
        // BUILD_TAG (jenkins-<job>-<build#>), not GIT_COMMIT-BUILD_ID: the branch
        // job and the PR job build the same commit with equal BUILD_IDs on the
        // same docker daemon, and the name must differ between them.
        def testContainer = "llm-sandbox-test-${env.BUILD_TAG}"

        sh "docker build --target test -t ${testImage} ."

        try {
          // A crashed agent pod skips the finally-cleanup and leaves a container
          // holding this name - remove any leftover before running.
          sh "docker rm -f ${testContainer} 2>/dev/null || true"
          sh "docker run --name ${testContainer} ${testImage} pytest --junitxml=test_report.xml"
        } finally {
          sh "docker cp ${testContainer}:/app/test_report.xml test_report.xml || true"
          sh "docker rm -f ${testContainer} || true"
          junit testResults: 'test_report.xml', allowEmptyResults: true
        }
      }

      // main only: push BOTH images to ECR. Two tags each — `main` (moving, convenience)
      // and the full commit sha (immutable; what k8s manifests should pin, especially
      // SANDBOX_IMAGE — session pods pull with imagePullPolicy IfNotPresent, so a moving
      // tag goes stale on warm nodes).
      if (env.BRANCH_NAME == 'main') {
        withCredentials([string(credentialsId: 'aws_account_id', variable: 'aws_account_id')]) {
          def awsRegistry = "${env.aws_account_id}.dkr.ecr.eu-central-1.amazonaws.com"

          docker.withRegistry("https://${awsRegistry}", 'ecr:eu-central-1:ecr-credentials') {
            stage('Build & push service image') {
              sh "docker build --target prod \
                    -t ${awsRegistry}/llm-sandbox:${env.BRANCH_NAME} \
                    -t ${awsRegistry}/llm-sandbox:${scmVars.GIT_COMMIT} ."
              sh "docker push ${awsRegistry}/llm-sandbox:${env.BRANCH_NAME}"
              sh "docker push ${awsRegistry}/llm-sandbox:${scmVars.GIT_COMMIT}"
            }

            stage('Build & push runtime image') {
              sh "docker build -f sandbox.Dockerfile \
                    -t ${awsRegistry}/llm-sandbox-runtime:${env.BRANCH_NAME} \
                    -t ${awsRegistry}/llm-sandbox-runtime:${scmVars.GIT_COMMIT} ."
              sh "docker push ${awsRegistry}/llm-sandbox-runtime:${env.BRANCH_NAME}"
              sh "docker push ${awsRegistry}/llm-sandbox-runtime:${scmVars.GIT_COMMIT}"
            }
          }
        }
      }
    }
  }
}
