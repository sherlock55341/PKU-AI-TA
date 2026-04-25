# Rubric for Assignment #2

General grading rules:
- Each problem is worth 10 points, total 50 points.
- Award partial credit for mathematically correct ideas even if notation differs.
- Do not deduct for minor notation differences if the algorithm, update rule, and objective are clear.
- Deduct for using forbidden high-level optimization or clustering packages. NumPy is allowed; for Problem 5, plotting with matplotlib is acceptable only for visualization, not for clustering.

## Problem 1: Subgradient Method for the Primal Group Lasso (10 pts)

### Grading points
- 2 pts: Correctly states the primal objective
  \[
  F(x)=\frac12\|Ax-b\|_F^2+\mu\sum_{i=1}^n\|x_{i,:}\|_2.
  \]
- 2 pts: Correctly gives the gradient of the smooth term:
  \[
  \nabla \frac12\|Ax-b\|_F^2=A^T(Ax-b).
  \]
- 3 pts: Correctly describes a valid subgradient of the row-wise \(\ell_{1,2}\) term:
  \[
  g_{i,:}=\frac{x_{i,:}}{\|x_{i,:}\|_2}\quad\text{if }x_{i,:}\ne0,
  \]
  and any vector with norm at most 1 if \(x_{i,:}=0\).
- 2 pts: Gives a correct subgradient iteration:
  \[
  x^{k+1}=x^k-\alpha_k\left(A^T(Ax^k-b)+\mu g^k\right).
  \]
- 1 pt: Mentions a reasonable stepsize/stopping rule, such as diminishing stepsize \(\alpha_k=c/\sqrt{k+1}\), objective change, gradient mapping norm, or max iterations.

### Example answer
Initialize \(x^0=0\). At iteration \(k\), compute
\[
r^k=Ax^k-b,\qquad d^k=A^Tr^k+\mu g^k,
\]
where each row of \(g^k\) is
\[
g^k_{i,:}=
\begin{cases}
x^k_{i,:}/\|x^k_{i,:}\|_2, & x^k_{i,:}\ne0,\\
0, & x^k_{i,:}=0
\end{cases}
\]
with \(0\) being a valid subgradient at the origin. Then update
\[
x^{k+1}=x^k-\alpha_k d^k.
\]
Use a diminishing stepsize such as \(\alpha_k=c/\sqrt{k+1}\), record \(F(x^k)\), and stop when the relative objective change is small or the maximum number of iterations is reached.

## Problem 2: Proximal Gradient Method for the Primal Group Lasso (10 pts)

### Grading points
- 2 pts: Splits the objective into smooth and nonsmooth parts:
  \[
  f(x)=\frac12\|Ax-b\|_F^2,\qquad h(x)=\mu\|x\|_{1,2}.
  \]
- 2 pts: Correctly gives \(\nabla f(x)=A^T(Ax-b)\).
- 2 pts: States a valid stepsize rule, e.g. \(\alpha\le 1/L\), where \(L=\|A\|_2^2\), or uses backtracking.
- 3 pts: Correctly gives the row-wise group soft-thresholding proximal operator:
  \[
  x^{k+1}_{i,:}=\max\left(1-\frac{\alpha\mu}{\|y_{i,:}\|_2},0\right)y_{i,:},
  \quad y=x^k-\alpha A^T(Ax^k-b).
  \]
- 1 pt: Gives a sensible stopping criterion and objective tracking.

### Example answer
Let \(L=\|A\|_2^2\) and choose \(\alpha=1/L\). Starting from \(x^0=0\), repeat:
\[
y^k=x^k-\alpha A^T(Ax^k-b).
\]
For each row \(i\), apply the proximal operator of \(\alpha\mu\|\cdot\|_2\):
\[
x^{k+1}_{i,:}=
\begin{cases}
\left(1-\frac{\alpha\mu}{\|y^k_{i,:}\|_2}\right)y^k_{i,:}, & \|y^k_{i,:}\|_2>\alpha\mu,\\
0, & \|y^k_{i,:}\|_2\le\alpha\mu.
\end{cases}
\]
Stop when \(\|x^{k+1}-x^k\|_F/\max(1,\|x^k\|_F)\) or the relative objective change is below a tolerance.

## Problem 3: Augmented Lagrangian Method for the Dual Problem (10 pts)

### Grading points
- 2 pts: Correctly derives or states a valid dual. A convenient equivalent form is
  \[
  \min_y \frac12\|y-b\|_F^2
  \quad\text{s.t.}\quad
  \| (A^T y)_{i,:}\|_2\le\mu,\ i=1,\dots,n.
  \]
- 2 pts: Introduces an auxiliary variable \(z=A^Ty\) and the constraint set
  \[
  C=\{z:\|z_{i,:}\|_2\le\mu,\ \forall i\}.
  \]
- 2 pts: Writes the augmented Lagrangian:
  \[
  \mathcal L_\beta(y,z,\lambda)=
  \frac12\|y-b\|_F^2+I_C(z)+
  \langle\lambda,A^Ty-z\rangle+
  \frac\beta2\|A^Ty-z\|_F^2.
  \]
- 2 pts: Gives the correct update structure:
  solve a linear system for \(y\), project rows for \(z\), and update \(\lambda\).
- 1 pt: Correctly states the row-wise projection onto the \(\ell_2\) ball of radius \(\mu\).
- 1 pt: Mentions stopping criteria based on primal feasibility \(\|A^Ty-z\|_F\), dual objective, or relative changes.

### Example answer
Use the dual form
\[
\min_y \frac12\|y-b\|_F^2 \quad\text{s.t.}\quad A^Ty=z,\ z\in C,
\]
where \(C=\{z:\|z_{i,:}\|_2\le\mu\}\). For penalty parameter \(\beta>0\), repeat:
\[
(I+\beta AA^T)y^{k+1}=b-A\lambda^k+\beta A z^k.
\]
Then compute
\[
w=A^Ty^{k+1}+\lambda^k/\beta
\]
and project each row onto the \(\ell_2\) ball:
\[
z^{k+1}_{i,:}=
\begin{cases}
w_{i,:}, & \|w_{i,:}\|_2\le\mu,\\
\mu w_{i,:}/\|w_{i,:}\|_2, & \|w_{i,:}\|_2>\mu.
\end{cases}
\]
Finally update
\[
\lambda^{k+1}=\lambda^k+\beta(A^Ty^{k+1}-z^{k+1}).
\]
Track the dual objective and feasibility residual. The multiplier \(\lambda\) can also be used as a primal estimate under the KKT relationship.

## Problem 4: Implementation and Convergence Comparison (10 pts)

### Grading points
- 2 pts: Correctly generates the provided test data with the specified random seed and dimensions.
- 2 pts: Implements the subgradient method using only NumPy.
- 2 pts: Implements the proximal gradient method using only NumPy.
- 2 pts: Implements the augmented Lagrangian dual method using only NumPy.
- 1 pt: Compares convergence speeds using objective value, relative error/change, feasibility residual, iteration count, runtime, table, or plot.
- 1 pt: Provides a clear conclusion about relative performance and explains the observed behavior.

### Example answer
A good implementation should define:
```python
def objective(A, b, x, mu):
    return 0.5 * np.linalg.norm(A @ x - b, "fro")**2 + mu * np.sum(np.linalg.norm(x, axis=1))
```
Then run the three algorithms on:
```python
np.random.seed(998244353)
n, m, l, mu = 512, 256, 2, 1e-2
A = np.random.randn(m, n)
k = round(n * 0.1)
p = np.random.permutation(n)[:k]
u = np.zeros((n, l))
u[p, :] = np.random.randn(k, l)
b = A @ u
```
The comparison should report objective values versus iteration or time. A typical conclusion is that the subgradient method converges slowly and noisily, proximal gradient is usually faster and more stable for the nonsmooth primal problem, and ALM can converge well on feasibility/dual objective but each iteration may be more expensive due to solving a linear system.

## Problem 5: NumPy K-Means Clustering (10 pts)

### Grading points
- 2 pts: Correctly generates or uses the given synthetic 2D dataset with three Gaussian clusters.
- 2 pts: Correctly initializes three cluster centers, preferably with a reproducible random seed or by sampling points from the dataset.
- 2 pts: Correctly assigns each point to the nearest center using Euclidean distance.
- 2 pts: Correctly updates each center as the mean of points assigned to that cluster.
- 1 pt: Includes a convergence rule, such as unchanged labels, center movement below tolerance, or max iterations.
- 1 pt: Handles practical details such as empty clusters, NumPy-only implementation for clustering, and optional visualization of final labels.

### Example answer
Generate the data:
```python
np.random.seed(42)
cluster1 = np.random.normal(loc=[2, 2], scale=0.5, size=(50, 2))
cluster2 = np.random.normal(loc=[6, 6], scale=0.8, size=(50, 2))
cluster3 = np.random.normal(loc=[10, 2], scale=0.6, size=(50, 2))
X = np.vstack([cluster1, cluster2, cluster3])
```
Reference K-Means structure:
```python
def kmeans(X, k=3, max_iter=100, tol=1e-6, seed=0):
    rng = np.random.default_rng(seed)
    centers = X[rng.choice(len(X), size=k, replace=False)].copy()
    labels = np.zeros(len(X), dtype=int)

    for _ in range(max_iter):
        distances = np.linalg.norm(X[:, None, :] - centers[None, :, :], axis=2)
        new_labels = np.argmin(distances, axis=1)

        new_centers = centers.copy()
        for j in range(k):
            members = X[new_labels == j]
            if len(members) > 0:
                new_centers[j] = members.mean(axis=0)
            else:
                new_centers[j] = X[rng.integers(len(X))]

        if np.linalg.norm(new_centers - centers) < tol or np.array_equal(new_labels, labels):
            centers = new_centers
            labels = new_labels
            break

        centers = new_centers
        labels = new_labels

    return labels, centers
```
The final result should identify three clusters near \((2,2)\), \((6,6)\), and \((10,2)\), up to permutation of cluster labels.
