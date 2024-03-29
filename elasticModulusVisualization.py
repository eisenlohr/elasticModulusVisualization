#!/usr/bin/env python3

# data structure for unitsphere triangulation
# global array "connectivity" holding three node indices per triangle
# global array "node" containing the 3D coordinates
# global hash "nodeChild" with key made from both parents and child node index as value

import sys
import os
import argparse
import numpy as np
import vtk
import colormaps

from pathlib import Path


node = np.array( [
    [ 1.0, 0.0, 0.0], # 0
    [-1.0, 0.0, 0.0], # 1
    [ 0.0, 1.0, 0.0], # 2
    [ 0.0,-1.0, 0.0], # 3
    [ 0.0, 0.0, 1.0], # 4
    [ 0.0, 0.0,-1.0]  # 5
    ] )

octahedron = np.array( [
    [ 0, 2, 4 ],
    [ 2, 1, 4 ],
    [ 1, 3, 4 ],
    [ 3, 0, 4 ],
    [ 0, 5, 2 ],
    [ 2, 5, 1 ],
    [ 1, 5, 3 ],
    [ 3, 5, 0 ],
    ] )

def normalize(v):
   return v/np.linalg.norm(v,axis=-1,keepdims=True)

def iszero(a):
  return np.isclose(a,0.0,atol=1.0e-300,rtol=0.0)

def isone(a):
  return np.isclose(a,1.0,atol=1.0e-15,rtol=0.0)


def om2ax(om):
  """Orientation matrix to axis--angle."""
  P=-1
  ax=np.empty(4)

  # first get the rotation angle
  t = 0.5*(om.trace()-1.0)
  ax[3] = np.arccos(np.clip(t,-1.0,1.0))

  if iszero(ax[3]):
    ax = [ 0.0, 0.0, 1.0, 0.0]
  else:
    w,vr = np.linalg.eig(om)
  # next, find the eigenvalue (1,0j)
    i = np.where(np.isclose(w,1.0+0.0j))[0][0]
    ax[0:3] = np.real(vr[0:3,i])
    diagDelta = np.array([om[1,2]-om[2,1],om[2,0]-om[0,2],om[0,1]-om[1,0]])
    ax[0:3] = np.where(iszero(diagDelta), ax[0:3],np.abs(ax[0:3])*np.sign(-P*diagDelta))

  return np.array(ax)


def Sierpinsky(N=0):
    """
    Subdivide a triangle N times.

    Parameters
    ----------
    N : int
        Number of subdivision steps.
        Defaults to 0.

    Returns
    -------
    w : array of float, shape (:,3)
        Contribution (weight) of the three original triangle vertices to each node,
        i.e. points = einsum('im,mj',w,vertices)
    c : array of int, shape (:,3)
        Connectivity of resulting triangles.

    """
    n = 1+2**N
    r = np.arange(n)
    tril_n  = np.tril(np.ones((n,n),dtype=bool))
    tril_2n = np.tril(np.ones((2*n,2*n),dtype=bool))
    k,l = np.meshgrid(r,r,indexing='ij')
    i,j = k[tril_n],l[tril_n]
    c = l+np.atleast_2d(np.cumsum(r)).T
    return (
       np.array([n-1-i,i-j,j]).T/(n-1),
       np.array([[c,np.roll(c,-1,axis=0),np.roll(np.roll(c,-1,axis=0),-1,axis=1)],
                 [c,np.roll(np.roll(c,-1,axis=0),-1,axis=1),np.roll(c,-1,axis=1)],
                ]).transpose(2,3,0,1).reshape((n,2*n,3))
                  [:-1]
                  [np.broadcast_to(np.tril(np.ones((2*n,2*n),dtype=bool))[:-2:2,:,np.newaxis],(n-1,2*n,3))]
                  .reshape((-1,3))
    )


def inverse66(M66):
    """
    Invert tensor given in Voigt notation.

    Parameters
    ----------
    M66 : numpy.array (6,6)
        Tensor in Voigt notation.

    Returns
    -------
    I66 : numpy.array (6,6)
        Inverse of M66 in Voigt notation.

    References
    ----------
    http://citeseerx.ist.psu.edu/viewdoc/download?doi=10.1.1.622.4732&rep=rep1&type=pdf

    """
    W = np.identity(6)
    W[3,3] = W[4,4] = W[5,5] = 0.5
    return np.einsum('ij,jk,kl',W,np.linalg.inv(M66),W)


def C66toC3333(stiffness):
    """
    Expand stiffness tensor from contracted Voigt notation to full fourth-rank representation.

    Parameters
    ----------
    stiffness : numpy.array (6,6)
        Stiffness tensor in Voigt notation.

    Returns
    -------
    C3333 : numpy.array (3,3,3,3)
        Fourth-rank stiffness tensor.

    """
    index = np.array([[0,0],[1,1],[2,2],[1,2],[0,2],[0,1]])
    C3333 = np.zeros((3,3,3,3))
    for a in range(6):
      i,j = index[a]
      for b in range(6):
        k,l = index[b]
        C3333[i,j,k,l] = \
        C3333[i,j,l,k] = \
        C3333[j,i,k,l] = \
        C3333[j,i,l,k] = stiffness[a,b]

    return C3333


def E_hkl3333(S3333,dir):

    return 1./np.einsum('...i,...j,...k,...l,ijkl',dir,dir,dir,dir,S3333)


def C66fromSymmetry(c11=0.0,c12=0.0,c13=0.0,c14=0.0,c15=0.0,c16=0.0,
                            c22=0.0,c23=0.0,c24=0.0,c25=0.0,c26=0.0,
                                    c33=0.0,c34=0.0,c35=0.0,c36=0.0,
                                            c44=0.0,c45=0.0,c46=0.0,
                                                    c55=0.0,c56=0.0,
                                                            c66=0.0,
                    symmetry=None,
                    ):
    """
    Return symmetrized stiffness tensor based on given component values and crystal symmetry.

    Parameters
    ----------
    cAB : float , optional
        Value of stiffness tensor in Voigt notation at index A,B.
    symmetry : str , optional
        Crystal lattice symmetry.

    Returns
    -------
    C66 : numpy.array (6,6)
        Symmetrized stiffness tensor in Voigt notation.

    References
    ----------
    RFS Hearmon, The Elastic Constants of Anisotropic Materials, Reviews of Modern Physics 18 (1946) 409-440

    """
    C = np.zeros((6,6),dtype=float)

    if symmetry in ['isotropic','cubic','tetragonal','hexagonal','orthorhombic','monoclinic','triclinic']:
        C[0,0] = C[1,1] = C[2,2] = c11
        C[3,3] = C[4,4] = C[5,5] = 0.5*(c11-c12)

        C[0,1] = C[0,2] = C[1,2] = \
        C[1,0] = C[2,0] = C[2,1] = c12

    if symmetry in ['cubic','tetragonal','hexagonal','orthorhombic','monoclinic','triclinic']:
        C[3,3] = C[4,4] = C[5,5] = c44 if c44 > 0.0 else C[3,3]

    if symmetry in ['tetragonal','hexagonal','orthorhombic','monoclinic','triclinic']:
        C[2,2]                   = c33 if c33 and c33 > 0.0 else C[0,0]
        C[5,5]                   = c66 if c66 and c66 > 0.0 else C[3,3]

        C[0,2] = C[1,2]          = \
        C[2,0] = C[2,1]          = c13 if c13 and c13 > 0.0 else C[0,2]
        C[0,5]                   = c16 if c16 and c16 > 0.0 else 0.0
        C[5,0]                   = -c16 if c16 and c16 > 0.0 else 0.0

    if symmetry in ['hexagonal','orthorhombic','monoclinic','triclinic']:
        C[5,5]                   = 0.5*(c11-c12)

    if symmetry in ['orthorhombic','monoclinic','triclinic']:
        C[1,1]                   = c22 if c22 and c22 > 0.0 else C[0,0]
        C[2,2]                   = c33 if c33 and c33 > 0.0 else C[0,0]
        C[4,4]                   = c55 if c55 and c55 > 0.0 else C[3,3]
        C[5,5]                   = c66 if c66 and c66 > 0.0 else C[3,3]
        C[1,2] = C[2,1]          = c23 if c23 and c23 > 0.0 else C[1,2]

    if symmetry in ['monoclinic','triclinic']:
        C[1,5] = C[5,1]          = c26 if c26 and c26 > 0.0 else 0.0
        C[2,5] = C[5,2]          = c36 if c36 and c36 > 0.0 else 0.0
        C[3,4] = C[4,3]          = c45 if c45 and c45 > 0.0 else 0.0

    if symmetry in ['triclinic']:
        C[0,3] = C[3,0]          = c14 if c14 and c14 > 0.0 else 0.0
        C[0,4] = C[4,0]          = c15 if c15 and c15 > 0.0 else 0.0
        C[1,3] = C[3,1]          = c24 if c24 and c24 > 0.0 else 0.0
        C[1,4] = C[4,1]          = c25 if c25 and c25 > 0.0 else 0.0
        C[2,3] = C[3,2]          = c34 if c34 and c34 > 0.0 else 0.0
        C[2,4] = C[4,2]          = c35 if c35 and c35 > 0.0 else 0.0
        C[2,5] = C[5,2]          = c36 if c36 and c36 > 0.0 else 0.0
        C[3,5] = C[5,3]          = c46 if c46 and c46 > 0.0 else 0.0
        C[4,5] = C[5,4]          = c56 if c56 and c56 > 0.0 else 0.0

    return C


def vtk_writeData(filename,coords,connectivity):
    """
    Write a VTK PolyData object of the directional elastic modulus.

    Parameters
    ----------
    filename : str
        Name of output file. Extension will be replaced by VTK default.
    coords : array of float, shape (N,3)
        Coordinates of points.
    connectivity : array of int, shape (M,3)
        Node indices per each triangle.

    """
    polydata = vtk.vtkPolyData()
    triangles = vtk.vtkCellArray()
    triangle = vtk.vtkTriangle()
    magnitude = vtk.vtkDoubleArray()
    magnitude.SetNumberOfComponents(1)
    magnitude.SetName("E");

    points = vtk.vtkPoints()
    for p in coords:
        points.InsertNextPoint(*p)
        magnitude.InsertNextValue(np.linalg.norm(p))
        polydata.GetPointData().AddArray(magnitude)

    for t in connectivity:
        for c in range(3):
            triangle.GetPointIds().SetId(c, t[c])
        triangles.InsertNextCell(triangle)

    polydata.SetPoints(points)
    polydata.SetPolys(triangles)

    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(Path(filename).with_suffix('.'+writer.GetDefaultFileExtension())))
    writer.SetInputData(polydata)
    writer.Write()


def x3d_writeData(filename,coords,connectivity):
    """
    Write a HTML page that interactively visualizes the directional elastic modulus.

    Parameters
    ----------
    filename : str
        Name of output file. Extension will be replaced by 'html'.
    coords : array of float, shape (N,3)
        Coordinates of points.
    connectivity : array of int, shape (M,3)
        Node indices per each triangle.

    """
    ax = om2ax(np.array([[-1., 1., 0.],
                         [-1.,-1., 2.],
                         [ 1., 1., 1.],
                        ])/np.array([np.sqrt(2.),np.sqrt(6.),np.sqrt(3.)])[:,None])

    auto = np.max(np.linalg.norm(coords,axis=-1))
    minimum = np.min(np.linalg.norm(coords,axis=-1))

    m = colormaps.Colormap(predefined=args.colormap)
    if args.invert:
        m = m.invert()

    output = [
    """
<html>
<head>
  <title>Elastic Tensor visualization</title>
  <script type='text/javascript' src='https://www.x3dom.org/download/x3dom.js'> </script>
  <link rel='stylesheet' type='text/css' href='https://www.x3dom.org/download/x3dom.css'></link>
</head>
<body>
  <h1>Elastic Tensor visualization</h1>
  <p>
  Range goes from {min} to {max}
  </p>
  <x3d width='600px' height='600px'>
  <scene>
    <viewpoint position='{view} {view} {view}' orientation='{axis[0]} {axis[1]} {axis[2]} {angle}'></viewpoint>
    <transform translation='{scale} 0 0' rotation='0 0 1 1.5708'>
    <shape>
      <appearance>
      <material diffuseColor='1 0 0'></material>
      </appearance>
      <cylinder radius='{radius}' height='{height}'></cylinder>
    </shape>
    </transform>
    <transform translation='0 {scale} 0'>
    <shape>
      <appearance>
      <material diffuseColor='0 1 0'></material>
      </appearance>
      <cylinder radius='{radius}' height='{height}'></cylinder>
    </shape>
    </transform>
    <transform translation='0 0 {scale}' rotation='1 0 0 1.5708'>
    <shape>
      <appearance>
      <material diffuseColor='0 0 1'></material>
      </appearance>
      <cylinder radius='{radius}' height='{height}'></cylinder>
    </shape>
    </transform>

    <shape>
      <appearance>
      <material diffuseColor="0.3 0.6 0.2"
                ambientIntensity="0.167"
                shininess="0.17"
                transparency="0.0"
       />
      </appearance>

      <IndexedFaceSet solid="false"
                      convex="true"
                      colorPerVertex="true"
                      creaseAngle="0.0"
                      coordIndex="
  """.format(min=minimum,max=auto,scale=1.5*auto,view=3*auto,axis=ax[:3],angle=ax[3],radius=auto/50.,height=auto)
  ] + \
  [' '.join(map(str,v))+' -1,' for v in connectivity] + \
  [
  """
        ">
        <coordinate point="
  """] + \
  [' '.join(map(str,v)) + ', ' for  v in coords] + \
  ["""
        "></coordinate>
        <color color="
  """] + \
  ['{} {} {}'.format(*(m.color(fraction=np.linalg.norm(v)/auto).expressAs('RGB').color)) + ', ' for  v in coords] + \
  [
  """
        "></color>

      </IndexedFaceSet>
    </shape>
  </scene>
  </x3d>
</body>
</html>
    """]

    with open(Path(filename).with_suffix('.html'),'w') as f:
        f.write('\n'.join(output) + '\n')


parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('format',
                    help='output file format',
                    choices=['vtk','x3d'])
parser.add_argument('name', help='output file name')
parser.add_argument('-c', '--colormap',
                    help='colormap for visualization',
                    choices=colormaps.Colormap().predefined(), default='seaweed')
parser.add_argument('-i', '--invert',
                    help='invert colormap',
                    action='store_true')
parser.add_argument('-N', '--recursion',
                    help='number of recursive refinement steps',
                    type=int, default=5)
parser.add_argument('--symmetry',
                    help='crystal structure symmetry',
                    default='isotropic',
                    choices=['triclinic',
                             'monoclinic',
                             'orthorhombic',
                             'hexagonal',
                             'tetragonal',
                             'cubic',
                             'isotropic',
                            ])
for i in range(6):
    for j in range(i,6):
        parser.add_argument(f'--c{i+1}{j+1}', type=float, required=i==0 and j<2)

args = parser.parse_args()

S3333 = C66toC3333(inverse66(C66fromSymmetry(c11 = args.c11,
                                             c12 = args.c12,
                                             c13 = args.c13,
                                             c14 = args.c14,
                                             c15 = args.c15,
                                             c16 = args.c16,
                                             c22 = args.c22,
                                             c23 = args.c23,
                                             c24 = args.c24,
                                             c25 = args.c25,
                                             c26 = args.c26,
                                             c33 = args.c33,
                                             c34 = args.c34,
                                             c35 = args.c35,
                                             c36 = args.c36,
                                             c44 = args.c44,
                                             c45 = args.c45,
                                             c46 = args.c46,
                                             c55 = args.c55,
                                             c56 = args.c56,
                                             c66 = args.c66,
                                             symmetry = args.symmetry,
                                            )))

N_faces = len(octahedron)
weights,c = Sierpinsky(N=args.recursion)

connectivity = (np.broadcast_to(len(weights)*np.arange(N_faces)[:,np.newaxis,np.newaxis],
                                (N_faces,)+c.shape)+c).reshape((-1,3))
nodes = normalize(np.einsum('ik,...kj',weights,node[octahedron])).reshape((-1,3))
nodes *= E_hkl3333(S3333,nodes)[...,np.newaxis]

{'vtk': vtk_writeData,
 'x3d': x3d_writeData,
}[args.format](args.name,nodes,connectivity)
